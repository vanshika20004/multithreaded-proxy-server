import threading
from flask import Flask, request, jsonify
from flask_cors import CORS

from proxy_server import ProxyServer
from utils.cache_manager import list_cache, clear_cache
from utils.filter_manager import view_blocked, add_blocked, remove_blocked
from utils.logger_manager import view_logs, clear_logs

app = Flask(__name__)
CORS(app)

proxy_server = ProxyServer()
status_lock = threading.Lock()


@app.route('/control/start', methods=['POST'])
def start_proxy():
    with status_lock:
        if proxy_server.running:
            return jsonify({'message': 'Proxy already running', 'running': True}), 200
        ok = proxy_server.start()
        msg = 'Proxy started' if ok else f"Failed to start proxy: {proxy_server.last_error or 'unknown error'}"
        # Fallback ports if default is busy
        if not ok:
            for fallback in (8081, 8888, 9000):
                proxy_server.port = fallback
                ok = proxy_server.start()
                if ok:
                    msg = f"Proxy started on fallback port {fallback} (8080 was busy)"
                    break
    return jsonify({'message': msg, 'running': ok, 'port': proxy_server.port}), (200 if ok else 500)


@app.route('/control/stop', methods=['POST'])
def stop_proxy():
    with status_lock:
        if not proxy_server.running:
            return jsonify({'message': 'Proxy already stopped', 'running': False}), 200
        proxy_server.stop()
    return jsonify({'message': 'Proxy stopped', 'running': False}), 200


@app.route('/status', methods=['GET'])
def status():
    cache_entries = list_cache()
    blocked = view_blocked()
    return jsonify({
        'running': proxy_server.running,
        'active_threads': proxy_server.get_active_thread_count(),
        'cache_entries': len(cache_entries),
        'blocked_count': len(blocked),
        'listening_port': proxy_server.port
    })


# Logs
@app.route('/logs/view', methods=['GET'])
def logs_view():
    return jsonify({'logs': view_logs()})


@app.route('/logs/clear', methods=['POST'])
def logs_clear():
    clear_logs()
    return jsonify({'message': 'Logs cleared'})


# Cache
@app.route('/cache/view', methods=['GET'])
def cache_view():
    return jsonify({'cache': list_cache()})


@app.route('/cache/clear', methods=['POST'])
def cache_clear():
    clear_cache()
    return jsonify({'message': 'Cache cleared'})


# Filter
@app.route('/filter/view', methods=['GET'])
def filter_view():
    return jsonify({'blocked': view_blocked()})


@app.route('/filter/add', methods=['POST'])
def filter_add():
    data = request.get_json(silent=True) or {}
    domain = str(data.get('domain', '')).strip()
    if not domain:
        return jsonify({'error': 'Domain required'}), 400
    add_blocked(domain)
    return jsonify({'message': 'Domain added', 'blocked': view_blocked()})


@app.route('/filter/remove', methods=['POST'])
def filter_remove():
    data = request.get_json(silent=True) or {}
    domain = str(data.get('domain', '')).strip()
    if not domain:
        return jsonify({'error': 'Domain required'}), 400
    remove_blocked(domain)
    return jsonify({'message': 'Domain removed', 'blocked': view_blocked()})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)