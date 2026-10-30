"""
Dashboard de monitorização do Visa Bot — Flask + SocketIO (WebSocket).

CORRE NO MESMO PROCESSO do bot (numa thread daemon), lê diretamente as globais
_live_status e _sentinel da memória do processo. Zero latência, zero infra extra.

Não toca na lógica do bot — é uma camada de visualização read-only por cima do
estado que o bot já mantém. Se o dashboard falhar, o bot continua a correr.

Integração em api_bot.py (mínima, defensiva):
  - No topo:        import dashboard
  - No main():      dashboard.start_in_thread(DASHBOARD_TOKEN)
  - Em _update_live_status / _clear_live_status / _signal_*:
                    dashboard.emit_update()  (dentro de try/except)

Endpoints:
  GET  /?token=XXX          — página HTML (dark mode, tabela de contas)
  GET  /api/state?token=XXX — JSON completo (live_status + sentinel + métricas)
  WS   /socket.io           — push instantâneo quando o estado muda
"""
from __future__ import annotations

import os
import threading
import time
import logging
from collections import deque
from typing import Any, Dict, List, Optional

from flask import Flask, request, jsonify, render_template_string
from flask_socketio import SocketIO

logger = logging.getLogger('VisaAPIv3').getChild('Dashboard')
# Carregar o HTML do dashboard a partir do arquivo externo
with open(os.path.join(os.path.dirname(__file__), 'dashboard.html'), 'r', encoding='utf-8') as f:
    _DASHBOARD_HTML = f.read()

# =================================================================================
# ESTADO LOCAL DO DASHBOARD — buffer de eventos + referências lazy às globais do bot
# =================================================================================
# O dashboard mantém o seu próprio buffer de eventos (timeline) porque as globais
# _live_status / _sentinel vivem no módulo api_bot e são lidas via lazy import.
# Isto evita dependência circular: api_bot importa dashboard, dashboard NÃO importa api_bot.

_event_buffer: deque = deque(maxlen=200)  # timeline dos últimos 200 eventos
_event_lock = threading.Lock()
_socketio: Optional[SocketIO] = None
_auth_token: Optional[str] = None
_start_time: float = time.time()

# Referências lazy às globais do bot (preenchidas por set_state_refs no arranque)
_live_status_ref: Dict = {}
_sentinel_ref: Dict = {}
_format_status_table_ref = None  # função format_status_table do api_bot


def set_state_refs(live_status: Dict, sentinel: Dict, format_fn=None):
    """O api_bot chama isto no arranque para partilhar as suas globais connosco.
    Sem isto, o dashboard não consegue ler o estado — mas não há import circular."""
    global _live_status_ref, _sentinel_ref, _format_status_table_ref
    _live_status_ref = live_status
    _sentinel_ref = sentinel
    _format_status_table_ref = format_fn


# =================================================================================
# CONTROLO — pause/resume + forçar signal sentinela (thread-safe)
# =================================================================================
# O dashboard corre numa thread Flask; o bot corre no loop asyncio principal.
# Para comunicar entre threads usamos:
#   - _bot_paused: threading.Event (thread-safe por design) — lido pelo bot
#   - _bot_loop: referência ao asyncio loop do bot — para call_soon_threadsafe
# call_soon_threadsafe é a forma correta de agendar código no loop asyncio
# a partir de outra thread (chamar asyncio.Event.set() diretamente de outra
# thread não é seguro em CPython).

_bot_loop = None               # asyncio.AbstractEventLoop do bot
_bot_paused = threading.Event()  # set = pausado (não claim novas contas)


def set_event_loop(loop):
    """O api_bot chama isto no main() para partilhar o seu event loop connosco.
    Necessário para call_soon_threadsafe (forçar signal / re-eleição)."""
    global _bot_loop
    _bot_loop = loop


def is_paused() -> bool:
    """O bot chama isto no loop principal. True = não claim novas contas."""
    return _bot_paused.is_set()


def pause_bot():
    """Pausa o bot: não claim novas contas. As contas em curso continuam até terminar."""
    _bot_paused.set()
    _append_event('control', 'BOT PAUSED via dashboard — não vai claim novas contas (em curso continuam)', '', '')
    emit_update('control', 'BOT PAUSED via dashboard')


def resume_bot():
    """Resume o bot: volta a claim novas contas no próximo ciclo."""
    _bot_paused.clear()
    _append_event('control', 'BOT RESUMED via dashboard — a claim novas contas', '', '')
    emit_update('control', 'BOT RESUMED via dashboard')


def force_signal_slots():
    """Força o sinal 'slots found' da sentinela — todos os followers disparam /slots.
    Usa call_soon_threadsafe porque asyncio.Event.set() não é thread-safe.
    Cuidado: se NÃO houver vagas reais, cada follower gasta 1 captcha em vão."""
    if _bot_loop is None or not _sentinel_ref:
        return False, 'event loop ou sentinela não disponível'
    def _do():
        _sentinel_ref['found'] = True
        _sentinel_ref['slot_data'] = []  # followers vão fazer /slots e ver o que aparece
        _sentinel_ref['event'].set()
    _bot_loop.call_soon_threadsafe(_do)
    _append_event('control', 'FORCE SIGNAL disparado — todos os followers vão disparar /slots', '', '')
    emit_update('control', 'FORCE SIGNAL disparado via dashboard')
    return True, 'signal disparado'


def force_sentinel_reelection():
    """Força a morte da sentinela atual → followers fazem re-eleição.
    Útil se a sentinela estiver presa (sessão morta mas não detetada)."""
    if _bot_loop is None or not _sentinel_ref:
        return False, 'event loop ou sentinela não disponível'
    old_sentinel = _sentinel_ref.get('sentinel_id', '?')
    def _do():
        _sentinel_ref['found'] = False
        _sentinel_ref['sentinel_id'] = None
        _sentinel_ref['event'].set()
    _bot_loop.call_soon_threadsafe(_do)
    _append_event('control', f'FORCE RE-ELECTION — sentinela {old_sentinel} forçada a morte, followers vão re-eleger', '', '')
    emit_update('control', f'FORCE RE-ELECTION — sentinela {old_sentinel} morta')
    return True, f'sentinela {old_sentinel} morta, re-eleição em curso'


# =================================================================================
# BUFFER DE EVENTOS (timeline) — alimentado por emit_update()
# =================================================================================
def _append_event(event_type: str, detail: str, username: str = '', stage: str = ''):
    """Adiciona um evento ao buffer da timeline (chamado pelo bot via emit_update)."""
    evt = {
        'ts': time.time(),
        'type': event_type,
        'detail': (detail or '')[:200],
        'username': username,
        'stage': stage or '',
    }
    with _event_lock:
        _event_buffer.append(evt)


def get_events(limit: int = 50) -> List[Dict]:
    with _event_lock:
        items = list(_event_buffer)
    return items[-limit:]


# =================================================================================
# SNAPSHOT — lê as globais do bot e constrói o JSON para a UI
# =================================================================================
def _build_snapshot() -> Dict[str, Any]:
    """Constrói um snapshot completo do estado do bot para a UI/API/WebSocket."""
    now = time.time()
    accounts: List[Dict] = []
    for acc_id, info in _live_status_ref.items():
        accounts.append({
            'id': acc_id,
            'username': info.get('username', '?'),
            'stage': info.get('stage', '-'),
            'event': info.get('event', '-'),
            'detail': (info.get('detail') or '')[:120],
            'age_s': round(now - info.get('updated_at', now), 1),
        })
    # Ordenar por username para a tabela ser estável
    accounts.sort(key=lambda a: str(a.get('username') or '~'))

    # Contadores globais por stage
    stage_counts: Dict[str, int] = {}
    for a in accounts:
        s = a['stage']
        stage_counts[s] = stage_counts.get(s, 0) + 1

    # Estado da sentinela
    sentinel_state = {
        'sentinel_id': _sentinel_ref.get('sentinel_id'),
        'generation': _sentinel_ref.get('generation', 0),
        'found': _sentinel_ref.get('found', False),
        'has_slot_data': _sentinel_ref.get('slot_data') is not None,
    }

    # Identificar a conta que é sentinela (para highlight na tabela)
    sentinel_username = None
    if sentinel_state['sentinel_id']:
        for a in accounts:
            if a['id'] == sentinel_state['sentinel_id']:
                sentinel_username = a['username']
                break

    return {
        'ts': now,
        'uptime_s': round(now - _start_time, 1),
        'accounts': accounts,
        'accounts_total': len(accounts),
        'stage_counts': stage_counts,
        'sentinel': sentinel_state,
        'sentinel_username': sentinel_username,
        'events': get_events(30),
        'paused': _bot_paused.is_set(),
    }


# =================================================================================
# EMIT — chamado pelo bot quando o estado muda (defensivo, nunca lança)
# =================================================================================
def emit_update(event_type: str = 'status', detail: str = '',
                username: str = '', stage: str = ''):
    """Chamado pelo bot (em _update_live_status / _clear_live_status / sinais sentinela)
    para (1) adicionar à timeline e (2) fazer push via WebSocket aos browsers conectados.

    É DEFENSIVO: se o SocketIO ainda não estiver pronto, ou se algo falhar, limita-se
    a logar em debug — nunca lança exceção para não quebrar o bot.
    """
    try:
        _append_event(event_type, detail, username, stage)
    except Exception:
        pass
    if _socketio is None:
        return
    try:
        _socketio.emit('state_update', _build_snapshot(), namespace='/')
    except Exception as exc:
        logger.debug(f"[Dashboard] emit falhou (non-fatal): {type(exc).__name__}: {exc}")


# =================================================================================
# FLASK APP
# =================================================================================
def _check_token() -> bool:
    """Verifica o token de auth. Vem de ?token=XXX na URL ou do header Authorization."""
    if not _auth_token:
        return True  # auth desativada
    # Query param
    tok = request.args.get('token', '')
    if tok and tok == _auth_token:
        return True
    # Header
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        if auth[7:] == _auth_token:
            return True
    # Cookie (para WebSocket reconectar sem reenviar token na query)
    tok = request.cookies.get('dash_token', '')
    if tok and tok == _auth_token:
        return True
    return False


def create_app(token: Optional[str]) -> Flask:
    """Cria a Flask app + SocketIO. Chamado por start_in_thread()."""
    global _auth_token, _socketio
    _auth_token = token

    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('DASHBOARD_SECRET', 'visa-bot-dash-default')
    socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*',
                        ping_timeout=60, ping_interval=25)
    _socketio = socketio

    # ---- Página HTML (dark mode, single-page) ----
    @app.route('/')
    def index():
        if not _check_token():
            # Mostra form de token se não vier na query
            return render_template_string(_LOGIN_HTML), 401
        # Setar cookie para WebSocket reconectar sem reenviar token
        resp = render_template_string(_DASHBOARD_HTML)
        return resp

    # ---- API JSON ----
    @app.route('/api/state')
    def api_state():
        if not _check_token():
            return jsonify({'error': 'unauthorized'}), 401
        return jsonify(_build_snapshot())

    @app.route('/api/events')
    def api_events():
        if not _check_token():
            return jsonify({'error': 'unauthorized'}), 401
        limit = int(request.args.get('limit', '50'))
        return jsonify({'events': get_events(limit)})

    @app.route('/health')
    def health():
        # Health check sem auth (para monitoring externo)
        return jsonify({'ok': True, 'uptime_s': round(time.time() - _start_time, 1)})

    # ---- Endpoints de CONTROLO (POST, com auth) ----
    @app.route('/api/control/<action>', methods=['POST'])
    def control(action):
        if not _check_token():
            return jsonify({'error': 'unauthorized'}), 401
        if action == 'pause':
            pause_bot()
            return jsonify({'ok': True, 'paused': True, 'msg': 'Bot pausado — não vai claim novas contas'})
        if action == 'resume':
            resume_bot()
            return jsonify({'ok': True, 'paused': False, 'msg': 'Bot resumido — a claim novas contas'})
        if action == 'force_signal':
            ok, msg = force_signal_slots()
            return jsonify({'ok': ok, 'msg': msg})
        if action == 'force_reelection':
            ok, msg = force_sentinel_reelection()
            return jsonify({'ok': ok, 'msg': msg})
        return jsonify({'error': f'unknown action: {action}'}), 400

    # ---- WebSocket connect ----
    @socketio.on('connect')
    def on_connect():
        # Validar token via query string do handshake do SocketIO
        tok = request.args.get('token', '')
        if _auth_token and tok != _auth_token:
            return False  # rejeita a conexão
        try:
            _socketio.emit('state_update', _build_snapshot(), to=request.sid)
        except Exception:
            pass

    return app


# =================================================================================
# ARRANQUE — chamado pelo api_bot no main()
# =================================================================================
_dash_thread: Optional[threading.Thread] = None


def start_in_thread(token: Optional[str] = None, host: str = '0.0.0.0',
                    port: int = 8080):
    """Arranca o Flask + SocketIO numa thread daemon. Non-blocking.

    O bot chama isto uma vez no main(). A thread corre para sempre em background.
    Se falhar, loga e continua — o bot não deve parar por causa do dashboard.
    """
    global _dash_thread
    if _dash_thread is not None and _dash_thread.is_alive():
        logger.warning("[Dashboard] já está a correr — ignorando start duplicado")
        return
    try:
        app = create_app(token)
        _dash_thread = threading.Thread(
            target=lambda: _socketio.run(app, host=host, port=port,
                                         allow_unsafe_werkzeug=True,
                                         log_output=False),
            name='Dashboard-Flask',
            daemon=True,
        )
        _dash_thread.start()
        logger.info(f"[Dashboard] arrancou em http://{host}:{port}/"
                    + (f"?token={token}" if token else ""))
    except Exception as exc:
        logger.error(f"[Dashboard] falha ao arrancar (non-fatal para o bot): "
                     f"{type(exc).__name__}: {exc}")


# =================================================================================
# HTML — dark mode (paleta do competidor), single-page, tabela de contas
# =================================================================================
_LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Visa Bot Dashboard</title>
<style>
body{font-family:-apple-system,Segoe UI,sans-serif;background:#0d1117;color:#c9d1d9;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{background:#161b22;border:1px solid #30363d;padding:2rem;border-radius:8px;text-align:center}
input{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:.6rem 1rem;
border-radius:6px;width:240px;font-size:14px}
button{background:#1f6feb;color:#fff;border:none;padding:.6rem 1.2rem;border-radius:6px;
cursor:pointer;margin-left:.5rem;font-size:14px}
button:hover{background:#388bfd}
</style></head><body><div class="box">
<h2>⚡ Visa Bot Dashboard</h2><p style="color:#8b949e">Token necessário</p>
<form onsubmit="location='/?token='+encodeURIComponent(this.t.value);return false">
<input name="t" placeholder="token" autofocus>
<button type="submit">Entrar</button></form></div></body></html>"""

# (continua na PARTE 2 — o _DASHBOARD_HTML grande vem a seguir)
# PLACEHOLDER_PARTE2