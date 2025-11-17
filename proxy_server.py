import socket
import threading
import time
import select
from urllib.parse import urlparse

from utils.cache_manager import get_from_cache, add_to_cache
from utils.filter_manager import is_blocked
from utils.logger_manager import log_action


class ProxyServer:
    def __init__(self, host='0.0.0.0', port=8080):
        self.host = host
        self.port = port
        self.server_socket = None
        self.running = False
        self.shutdown_event = threading.Event()
        self.client_threads = []
        self.client_threads_lock = threading.Lock()
        self.last_error = None

    def start(self):
        if self.running:
            return True
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(100)
            self.server_socket.settimeout(1.0)
            self.shutdown_event.clear()
            self.running = True
        except Exception as e:
            self.running = False
            self.server_socket = None
            self.last_error = str(e)
            return False

        threading.Thread(target=self._accept_loop, daemon=True).start()
        return True

    def stop(self):
        self.running = False
        self.shutdown_event.set()
        try:
            if self.server_socket:
                try:
                    self.server_socket.close()
                except Exception:
                    pass
        finally:
            self.server_socket = None

        # Join client threads
        with self.client_threads_lock:
            threads = list(self.client_threads)
        for t in threads:
            if t.is_alive():
                t.join(timeout=2.0)

    def _accept_loop(self):
        while not self.shutdown_event.is_set():
            try:
                client_sock, client_addr = self.server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                # Socket closed during shutdown
                break
            except Exception:
                continue

            t = threading.Thread(target=self._handle_client, args=(client_sock, client_addr), daemon=True)
            with self.client_threads_lock:
                self.client_threads.append(t)
            t.start()

        # Cleanup: remove finished threads
        with self.client_threads_lock:
            self.client_threads = [th for th in self.client_threads if th.is_alive()]

    def get_active_thread_count(self):
        with self.client_threads_lock:
            alive = [t for t in self.client_threads if t.is_alive()]
        return len(alive)

    def _handle_client(self, client_sock: socket.socket, client_addr):
        client_ip = f"{client_addr[0]}:{client_addr[1]}"
        try:
            request_data = self._recv_until_double_crlf(client_sock)
            if not request_data:
                client_sock.close()
                return

            first_line, headers, body = self._parse_request(request_data)
            if first_line is None:
                client_sock.close()
                return

            method, url, version = first_line

            # Determine full URL and host
            host = headers.get('host')
            full_url = url
            parsed = urlparse(url if url.startswith('http://') else f"http://{host}{url}")
            full_url = parsed.geturl()

            # Handle HTTPS tunneling via CONNECT
            if method.upper() == 'CONNECT':
                # url is like 'host:port'
                target = url
                try:
                    tgt_host, tgt_port = target.split(':', 1)
                    tgt_port = int(tgt_port)
                except ValueError:
                    # invalid CONNECT line
                    resp = self._make_http_response(400, 'Bad Request', b"<h1>Bad CONNECT request</h1>")
                    try:
                        client_sock.sendall(resp)
                    except Exception:
                        pass
                    log_action(client_ip, target, 'error_bad_connect')
                    client_sock.close()
                    return

                # Filtering for CONNECT (HTTPS)
                if is_blocked(tgt_host):
                    response = self._make_http_response(403, 'Forbidden', b"<h1>Access Denied</h1>")
                    try:
                        client_sock.sendall(response)
                    except Exception:
                        pass
                    # Log as HTTPS URL for user-friendly display
                    log_action(client_ip, f"https://{tgt_host}/", 'blocked_connect')
                    client_sock.close()
                    return

                # Establish tunnel
                try:
                    upstream = socket.create_connection((tgt_host, tgt_port), timeout=10)
                except Exception:
                    resp = self._make_http_response(502, 'Bad Gateway', b"<h1>Bad Gateway</h1>")
                    try:
                        client_sock.sendall(resp)
                    except Exception:
                        pass
                    log_action(client_ip, f"https://{tgt_host}/", 'error_connect_upstream')
                    client_sock.close()
                    return

                # Signal tunnel established
                try:
                    client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    # Log immediately so it appears in real-time (show domain nicely)
                    log_action(client_ip, f"https://{tgt_host}/", 'connect')
                except Exception:
                    try:
                        upstream.close()
                    except Exception:
                        pass
                    client_sock.close()
                    return

                # Relay data bidirectionally until closure
                try:
                    self._relay_bidirectional(client_sock, upstream)
                finally:
                    try:
                        upstream.close()
                    except Exception:
                        pass
                    try:
                        client_sock.close()
                    except Exception:
                        pass
                log_action(client_ip, f"https://{tgt_host}/", 'tunnel')
                return

            # Filtering for plain HTTP
            if is_blocked(parsed.hostname or ''):
                response = self._make_http_response(403, 'Forbidden', b"<h1>Access Denied</h1>")
                try:
                    client_sock.sendall(response)
                except Exception:
                    pass
                log_action(client_ip, full_url, 'blocked')
                client_sock.close()
                return

            # Caching (GET only)
            if method.upper() == 'GET':
                cached = get_from_cache(full_url)
                if cached is not None:
                    try:
                        client_sock.sendall(cached)
                    except Exception:
                        pass
                    log_action(client_ip, full_url, 'cached')
                    client_sock.close()
                    return

            # Forward to origin
            origin_host = parsed.hostname
            origin_port = parsed.port or 80
            path = parsed.path or '/'
            if parsed.query:
                path += f"?{parsed.query}"

            # Log request arrival for real-time visibility
            log_action(client_ip, full_url, 'request_http')

            try:
                upstream = socket.create_connection((origin_host, origin_port), timeout=10)
            except Exception:
                resp = self._make_http_response(502, 'Bad Gateway', b"<h1>Bad Gateway</h1>")
                try:
                    client_sock.sendall(resp)
                except Exception:
                    pass
                log_action(client_ip, full_url, 'error_upstream_connect')
                client_sock.close()
                return

            # Build upstream request
            # Copy headers but adjust Host and Connection
            header_lines = []
            for k, v in headers.items():
                if k.lower() in ('proxy-connection', 'connection'):
                    continue
                if k.lower() == 'host':
                    continue
                header_lines.append(f"{k}: {v}")
            header_lines.append(f"Host: {parsed.netloc}")
            header_lines.append("Connection: close")

            request_line = f"{method} {path} {version}"
            upstream_request = (request_line + "\r\n" + "\r\n".join(header_lines) + "\r\n\r\n").encode('iso-8859-1')
            try:
                upstream.sendall(upstream_request)
                # For GET we typically don't have body; still forward if present
                if body:
                    upstream.sendall(body)
            except Exception:
                try:
                    upstream.close()
                except Exception:
                    pass
                resp = self._make_http_response(502, 'Bad Gateway', b"<h1>Bad Gateway</h1>")
                try:
                    client_sock.sendall(resp)
                except Exception:
                    pass
                log_action(client_ip, full_url, 'error_upstream_send')
                client_sock.close()
                return

            # Read response and relay
            response_data = b''
            try:
                upstream.settimeout(10)
                while True:
                    chunk = upstream.recv(4096)
                    if not chunk:
                        break
                    response_data += chunk
                    client_sock.sendall(chunk)
            except Exception:
                # Possibly partial response; still close
                pass
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass
                try:
                    client_sock.close()
                except Exception:
                    pass

            # Cache only successful GET responses
            if method.upper() == 'GET' and response_data:
                try:
                    add_to_cache(full_url, response_data)
                    log_action(client_ip, full_url, 'fetched')
                except Exception:
                    log_action(client_ip, full_url, 'fetched_no_cache')
            else:
                # Non-GET forwarded
                log_action(client_ip, full_url, 'forwarded')

        except Exception:
            try:
                client_sock.close()
            except Exception:
                pass

    @staticmethod
    def _recv_until_double_crlf(sock: socket.socket, timeout=5):
        sock.settimeout(timeout)
        data = b''
        try:
            while b"\r\n\r\n" not in data and len(data) < 1024 * 64:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except Exception:
            return b''
        return data

    @staticmethod
    def _parse_request(data: bytes):
        try:
            header_part, body = data.split(b"\r\n\r\n", 1)
        except ValueError:
            header_part = data
            body = b''
        lines = header_part.split(b"\r\n")
        if not lines:
            return None, {}, b''
        try:
            first = lines[0].decode('iso-8859-1')
            method, url, version = first.split(' ', 2)
        except Exception:
            return None, {}, b''

        headers = {}
        for raw in lines[1:]:
            try:
                s = raw.decode('iso-8859-1')
                if ':' in s:
                    k, v = s.split(':', 1)
                    headers[k.strip()] = v.strip()
            except Exception:
                continue

        return (method, url, version), headers, body

    @staticmethod
    def _make_http_response(status_code: int, reason: str, body: bytes):
        body = body or b''
        headers = [
            f"HTTP/1.1 {status_code} {reason}",
            "Content-Type: text/html; charset=utf-8",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        return ("\r\n".join(headers) + "\r\n\r\n").encode('iso-8859-1') + body

    @staticmethod
    def _relay_bidirectional(a: socket.socket, b: socket.socket, idle_timeout=15):
        try:
            a.setblocking(False)
            b.setblocking(False)
            while True:
                rlist, _, _ = select.select([a, b], [], [], idle_timeout)
                if not rlist:
                    break
                for s in rlist:
                    try:
                        data = s.recv(4096)
                    except Exception:
                        return
                    if not data:
                        return
                    if s is a:
                        try:
                            b.sendall(data)
                        except Exception:
                            return
                    else:
                        try:
                            a.sendall(data)
                        except Exception:
                            return
        except Exception:
            pass


if __name__ == '__main__':
    ps = ProxyServer()
    if ps.start():
        print(f"ProxyServer listening on {ps.host}:{ps.port}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Stopping proxy...")
            ps.stop()
    else:
        print("Failed to start ProxyServer")