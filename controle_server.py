from http.server import HTTPServer, BaseHTTPRequestHandler
import subprocess
import socket
import threading
import time
import json
import logging
import os
import urllib.request
import webbrowser
from collections import deque

PORT        = 7861
PYTHON      = r"C:\AI\RuinedFooocus\python_embeded\python.exe"
SCRIPT      = r"C:\AI\RuinedFooocus\RuinedFooocus\entry_with_update.py"
WORKDIR     = r"C:\AI\RuinedFooocus\RuinedFooocus"
FOOOCUS_URL = "http://100.100.118.76:7860"
LOG_FILE    = r"C:\AI\RuinedFooocus\controle.log"
FOOOCUS_CONSOLE_LOG = r"C:\AI\RuinedFooocus\fooocus_console.log"

CREATE_NO_WINDOW = 0x08000000

# ── logging ────────────────────────────────────────────────────────────────────
_log_buffer = deque(maxlen=200)   # últimas 200 linhas em memória

class _BufHandler(logging.Handler):
    def emit(self, record):
        _log_buffer.append(self.format(record))

_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S')

logging.basicConfig(
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
    ]
)
_bh = _BufHandler()
_bh.setFormatter(_fmt)
logging.getLogger().addHandler(_bh)
for h in logging.getLogger().handlers:
    h.setFormatter(_fmt)

log = logging.getLogger(__name__)
log.info('=== Servidor de controle iniciado ===')

# ── estado global ──────────────────────────────────────────────────────────────
_lock     = threading.Lock()
_starting = False
_stopping = False


def port_open(host='127.0.0.1', port=7860, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def fooocus_alive(timeout=3.0):
    """Saúde REAL do Fooocus: porta aberta E servindo HTTP de verdade.
    port_open() só confirma o socket TCP — um processo travado/zumbi pode
    segurar a 7860 sem servir nada, e aí o status mentiria 'rodando'.
    Aqui confirmamos com um GET leve (não lemos o corpo) antes de afirmar
    que está no ar."""
    if not port_open(timeout=1.0):
        return False
    try:
        req = urllib.request.Request('http://127.0.0.1:7860/', method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 500
    except Exception:
        return False


def get_status():
    running = fooocus_alive()
    with _lock:
        return {
            'running':  running,
            'starting': _starting and not running,
            'stopping': _stopping and running,
        }


def start_fooocus():
    global _starting
    with _lock:
        if _starting:
            log.warning('start ignorado — já está iniciando')
            return False
        if fooocus_alive():
            log.warning('start ignorado — Fooocus já está rodando na porta 7860')
            return False
        _starting = True

    log.info('Iniciando Fooocus...')
    try:
        # Captura o stdout/stderr do Fooocus (log detalhado por imagem: passos, seed, tempo)
        # num arquivo, senao com CREATE_NO_WINDOW o terminal nao existe e o log se perde.
        # Visivel no celular via /flog. '-u' = sem buffer (atualiza em tempo real).
        try:
            _clog = open(FOOOCUS_CONSOLE_LOG, 'ab')
        except Exception:
            _clog = None
        proc = subprocess.Popen(
            [PYTHON, '-u', '-s', SCRIPT, '--listen', '--nobrowser'],
            cwd=WORKDIR,
            creationflags=CREATE_NO_WINDOW,
            # RF_OFFLINE=1 desliga o auto-update do git no boot (entry_with_update.py),
            # que daria reset --hard e apagaria as customizações deste projeto.
            env={**os.environ, 'RF_OFFLINE': '1'},
            stdout=_clog,
            stderr=subprocess.STDOUT,
        )
        log.info(f'Processo criado — PID {proc.pid}')
    except Exception as e:
        log.error(f'Falha ao criar processo: {e}')
        with _lock:
            _starting = False
        return False

    def _watch_start():
        global _starting
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(2)
            if fooocus_alive():
                log.info('Fooocus respondendo na porta 7860 — pronto')
                break
        else:
            log.error('Timeout: Fooocus não respondeu em 180s')
        with _lock:
            _starting = False

    threading.Thread(target=_watch_start, daemon=True).start()
    return True


def stop_fooocus():
    global _stopping, _starting
    with _lock:
        _starting = False
        _stopping = True

    log.info('Parando Fooocus — buscando PIDs...')
    ps = (
        "Get-WmiObject Win32_Process -Filter \"Name='python.exe'\" "
        "| Where-Object { $_.CommandLine -like '*entry_with_update*' } "
        "| Select-Object -ExpandProperty ProcessId"
    )
    try:
        r = subprocess.run(
            ['powershell', '-WindowStyle', 'Hidden', '-Command', ps],
            capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        pids = [p.strip() for p in r.stdout.splitlines() if p.strip().isdigit()]
        if not pids:
            log.warning('Nenhum processo entry_with_update encontrado via PowerShell')
        for pid in pids:
            log.info(f'Matando PID {pid} e filhos (taskkill /F /T)...')
            kr = subprocess.run(
                ['taskkill', '/F', '/T', '/PID', pid],
                capture_output=True, text=True,
                creationflags=CREATE_NO_WINDOW,
            )
            log.info(f'taskkill PID {pid}: {kr.stdout.strip() or kr.stderr.strip()}')
    except Exception as e:
        log.error(f'Erro ao matar processo: {e}')

    # fecha a janela aberta sobre a /tela — não deixa a TV órfã em "aguardando imagem"
    fechar_tela_no_pc()

    def _watch_stop():
        global _stopping
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(1)
            if not port_open():
                log.info('Porta 7860 fechada — Fooocus encerrado')
                break
        else:
            log.error('Timeout: porta 7860 ainda aberta após 30s')
        with _lock:
            _stopping = False

    threading.Thread(target=_watch_stop, daemon=True).start()


# ── abrir a visualização da imagem NA TELA DO PC ────────────────────────────────
# O Fooocus sobe com --nobrowser, então nada aparece no monitor do PC sozinho.
# Ao tocar "Controle remoto" no celular, o PC abre /tela em tela cheia (o óbvio:
# controlo do celular, vejo a imagem na tela grande). Usa um profile dedicado pra
# garantir uma janela nova em fullscreen sem mexer no Chrome normal do Átila.
TELA_URL_LOCAL = 'http://127.0.0.1:7860/tela'
_TELA_PROFILE  = r"C:\AI\RuinedFooocus\_tela_profile"
_BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

_ultima_abertura_tela = 0.0

def abrir_tela_no_pc():
    global _ultima_abertura_tela
    agora = time.time()
    if agora - _ultima_abertura_tela < 4.0:
        log.info('abrir-tela-pc ignorado — aberta há <4s (debounce anti-janela-dupla)')
        return True
    if not fooocus_alive():
        log.warning('abrir-tela-pc ignorado — Fooocus não está no ar')
        return False
    _ultima_abertura_tela = agora  # marca antes do Popen p/ não abrir 2 janelas em toque-duplo
    for b in _BROWSERS:
        if os.path.exists(b):
            try:
                subprocess.Popen([
                    b,
                    f'--user-data-dir={_TELA_PROFILE}',
                    '--no-first-run', '--no-default-browser-check',
                    '--new-window', '--start-fullscreen',
                    f'--app={TELA_URL_LOCAL}',
                ])
                log.info(f'Tela aberta no PC via {os.path.basename(b)} (app/fullscreen)')
                return True
            except Exception as e:
                log.warning(f'Falha ao abrir {b}: {e}')
    try:
        webbrowser.open(TELA_URL_LOCAL)
        log.info('Tela aberta no PC via navegador padrão (sem fullscreen garantido)')
        return True
    except Exception as e:
        log.error(f'Não consegui abrir a tela no PC: {e}')
        return False


def fechar_tela_no_pc():
    """Fecha a janela do navegador aberta SOBRE a /tela (usa o profile dedicado
    _tela_profile, então nunca encosta no Chrome normal do Átila). Chamado ao parar
    o Fooocus (manual ou auto-desligamento) — a tela não fica órfã mostrando 'aguardando'."""
    try:
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='msedge.exe'\" "
            "| Where-Object { $_.CommandLine -like '*_tela_profile*' } "
            "| Select-Object -ExpandProperty ProcessId"
        )
        r = subprocess.run(
            ['powershell', '-WindowStyle', 'Hidden', '-Command', ps],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        )
        pids = [p.strip() for p in r.stdout.splitlines() if p.strip().isdigit()]
        for pid in pids:
            subprocess.run(['taskkill', '/F', '/T', '/PID', pid],
                           capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        if pids:
            log.info(f'Tela do PC fechada (PIDs {pids})')
    except Exception as e:
        log.warning(f'Falha ao fechar a tela do PC: {e}')


# ── auto-desligamento por inatividade ───────────────────────────────────────────
# Pergunta do Átila: fechar o Fooocus sozinho após 5 min "sem uso". "Uso" = geração
# em curso OU a página /controle do celular aberta (ela manda heartbeat). A /tela
# (TV) NÃO conta — se ela ficar sozinha aberta e o celular for embora, desliga mesmo.
INATIVIDADE_LIMITE = 300  # segundos (5 min)

def _watch_inatividade():
    import json as _json
    while True:
        time.sleep(30)
        try:
            if not fooocus_alive():
                continue
            req = urllib.request.Request('http://127.0.0.1:7860/cr/idle', method='GET')
            with urllib.request.urlopen(req, timeout=3) as resp:
                d = _json.loads(resp.read().decode('utf-8', 'ignore'))
            if not d.get('gerando') and int(d.get('idle', 0)) >= INATIVIDADE_LIMITE:
                log.info(f"Auto-desligando Fooocus — {d.get('idle')}s sem uso (limite {INATIVIDADE_LIMITE}s)")
                stop_fooocus()  # já fecha a /tela junto
        except Exception as e:
            # Fooocus rodando versão antiga sem /cr/idle, ou rede: fail-safe = NÃO desliga
            log.debug(f'watch_inatividade: {e}')


# ── PWA manifest ───────────────────────────────────────────────────────────────
MANIFEST_JSON = json.dumps({
    "name": "RuinedFooocus",
    "short_name": "Fooocus",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0f0f13",
    "theme_color": "#0f0f13",
})

# ── HTML ───────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Fooocus</title>
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0f0f13">
<link rel="manifest" href="/manifest.json">
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{
  background:#0f0f13;color:#e0e0e0;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  min-height:100dvh;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:32px;padding:32px 20px;
}
.logo{font-size:22px;font-weight:700;letter-spacing:2px;color:#fff;text-transform:uppercase}
.logo span{color:#a78bfa}

.status-wrap{display:flex;flex-direction:column;align-items:center;gap:10px}
.dot{width:18px;height:18px;border-radius:50%;background:#6b7280;transition:background .4s}
.dot.on  {background:#22c55e;animation:pulse 2s infinite}
.dot.busy{background:#f59e0b;animation:blink .7s infinite}
.dot.off {background:#ef4444}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(34,197,94,.5)}70%{box-shadow:0 0 0 10px rgba(34,197,94,0)}100%{box-shadow:0 0 0 0 rgba(34,197,94,0)}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
.status-text{font-size:13px;color:#888;letter-spacing:1px;text-transform:uppercase;min-height:18px;text-align:center}

.btn-group{display:flex;flex-direction:column;gap:14px;width:100%;max-width:320px}
.btn{
  width:100%;padding:20px;border:none;border-radius:16px;
  font-size:17px;font-weight:600;cursor:pointer;
  transition:opacity .15s,transform .1s;user-select:none;
}
.btn:active:not(:disabled){opacity:.7;transform:scale(.97)}
.btn:disabled{opacity:.28;cursor:not-allowed;transform:none}
.btn-start{background:#a78bfa;color:#0f0f13}
.btn-stop {background:#1e1e2a;color:#ef4444;border:1.5px solid #ef4444}
.btn-open,
.btn-logs {
  background:#1e1e2a;text-decoration:none;display:block;text-align:center;
}
.btn-open{color:#a78bfa;border:1.5px solid #a78bfa}
.btn-logs{color:#6b7280;border:1.5px solid #374151;font-size:14px;padding:14px}

.toast{
  position:fixed;bottom:32px;left:50%;transform:translateX(-50%);
  padding:12px 28px;border-radius:99px;
  font-weight:600;font-size:14px;opacity:0;transition:opacity .35s;pointer-events:none;
  white-space:nowrap;
}
.toast.show{opacity:1}

/* logs modal */
.modal-bg{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);
  z-index:100;align-items:flex-end;
}
.modal-bg.open{display:flex}
.modal{
  background:#13131a;width:100%;max-height:70dvh;border-radius:20px 20px 0 0;
  padding:20px 16px 32px;display:flex;flex-direction:column;gap:12px;
}
.modal h2{font-size:14px;color:#6b7280;letter-spacing:1px;text-transform:uppercase}
.log-box{
  flex:1;overflow-y:auto;background:#0a0a10;border-radius:10px;
  padding:12px;font-family:monospace;font-size:11px;color:#9ca3af;
  white-space:pre-wrap;word-break:break-all;
}
.modal-close{
  background:#1e1e2a;color:#e0e0e0;border:none;border-radius:12px;
  padding:14px;font-size:15px;font-weight:600;cursor:pointer;
}
</style>
</head>
<body>
<div class="logo">Ruined<span>Fooocus</span></div>

<div class="status-wrap">
  <div class="dot" id="dot"></div>
  <div class="status-text" id="stxt">verificando...</div>
</div>

<div class="btn-group">
  <button class="btn btn-start" id="bStart" onclick="doStart()">Iniciar</button>
  <button class="btn btn-stop"  id="bStop"  onclick="doStop()">Parar</button>
  <a class="btn btn-open" id="bOpen" href="FOOOCUS_URL" target="_blank">Abrir interface →</a>
  <a class="btn btn-open" id="bCtrl" href="FOOOCUS_URL/controle" target="_blank" onclick="return abrirControleRemoto(event)">🎛️ Controle remoto (celular)</a>
  <button class="btn btn-logs" onclick="openLogs()">Ver logs do servidor</button>
  <button class="btn btn-logs" onclick="openFooocusLog()">Ver log detalhado do Fooocus</button>
</div>

<div class="toast" id="toast"></div>

<div class="modal-bg" id="modal">
  <div class="modal">
    <h2>Logs do servidor</h2>
    <div class="log-box" id="logbox">carregando...</div>
    <button class="modal-close" onclick="closeLogs()">Fechar</button>
  </div>
</div>

<script>
const URL_FOO = "FOOOCUS_URL";
const dot   = document.getElementById('dot');
const stxt  = document.getElementById('stxt');
const bStart= document.getElementById('bStart');
const bStop = document.getElementById('bStop');
const bOpen = document.getElementById('bOpen');
const bCtrl = document.getElementById('bCtrl');
const toast = document.getElementById('toast');

let pollTimer = null;
let elTimer   = null;
let elapsed   = 0;

function render(s) {
  if(!s.starting && !s.stopping) stopElapsed();
  if(!s.error) try{localStorage.setItem('_st',JSON.stringify(s));}catch{}
  if (s.error) {
    dot.className='dot'; stxt.textContent='sem conexão com o PC';
    lock(false,false,false); return;
  }
  if (s.stopping) {
    dot.className='dot busy'; stxt.textContent='parando...';
    lock(false,false,false); return;
  }
  if (s.starting) {
    dot.className='dot busy';
    if(!elTimer) startElapsed('iniciando');
    lock(false,false,false); return;
  }
  if (s.running) {
    stopElapsed();
    dot.className='dot on'; stxt.textContent='rodando';
    lock(false,true,true); return;
  }
  dot.className='dot off'; stxt.textContent='parado';
  lock(true,false,false);
}

function lock(start,stop,open){
  bStart.disabled=!start;
  bStop.disabled=!stop;
  [bOpen,bCtrl].forEach(b=>{b.style.pointerEvents=open?'auto':'none';b.style.opacity=open?'1':'0.28';});
}

function startElapsed(label){
  elapsed=0; stxt.textContent=label+'... 0s';
  elTimer=setInterval(()=>{elapsed++;stxt.textContent=label+'... '+elapsed+'s';},1000);
}
function stopElapsed(){clearInterval(elTimer);elTimer=null;}

function showToast(msg,color){
  toast.textContent=msg;
  toast.style.background=color||'#22c55e';
  toast.style.color=color?'#fff':'#0f0f13';
  toast.classList.add('show');
  setTimeout(()=>toast.classList.remove('show'),3500);
}

async function fetchStatus(){
  try{const r=await fetch('/status');return await r.json();}
  catch{return{error:true};}
}

function pollUntil(condFn,onDone,timeoutMs,intervalMs){
  stopPoll();
  const deadline=Date.now()+timeoutMs;
  pollTimer=setInterval(async()=>{
    const s=await fetchStatus();
    render(s);
    if(condFn(s)||Date.now()>deadline){stopPoll();onDone(s);}
  },intervalMs);
}
function stopPoll(){clearInterval(pollTimer);pollTimer=null;}

async function doStart(){
  lock(false,false,false);
  dot.className='dot busy'; startElapsed('iniciando');
  try{await fetch('/start',{method:'POST'});}
  catch{showToast('Erro ao conectar ao PC','#ef4444');fetchStatus().then(render);return;}
  pollUntil(
    s=>s.running,
    s=>{
      render(s);
      if(s.running){showToast('Fooocus pronto! Escolha o controle abaixo.');}
      else showToast('Timeout — abra os logs pra ver o erro','#ef4444');
    },180000,2500
  );
}

async function doStop(){
  lock(false,false,false);
  dot.className='dot busy'; stxt.textContent='enviando comando...';
  try{await fetch('/stop',{method:'POST'});}
  catch{showToast('Erro ao conectar ao PC','#ef4444');fetchStatus().then(render);return;}
  stxt.textContent='aguardando encerramento...';
  pollUntil(
    s=>!s.running&&!s.stopping,
    s=>{
      render(s);
      if(!s.running)showToast('Fooocus encerrado','#6b7280');
      else showToast('Não parou — abra os logs pra ver o erro','#ef4444');
    },30000,1500
  );
}

async function openLogs(){
  document.getElementById('modal').classList.add('open');
  const box=document.getElementById('logbox');
  box.textContent='carregando...';
  try{
    const r=await fetch('/logs');
    const d=await r.json();
    box.textContent=d.lines.join('\n')||'(sem logs ainda)';
    box.scrollTop=box.scrollHeight;
  }catch{box.textContent='Erro ao buscar logs';}
}
async function openFooocusLog(){
  document.getElementById('modal').classList.add('open');
  const box=document.getElementById('logbox');
  box.textContent='carregando...';
  try{
    const r=await fetch('/flog');
    box.textContent=await r.text()||'(sem log ainda)';
    box.scrollTop=box.scrollHeight;
  }catch{box.textContent='Erro ao buscar log do Fooocus';}
}
function closeLogs(){document.getElementById('modal').classList.remove('open');}

// ao tocar "Controle remoto" no celular: (1) manda o PC abrir a /tela em tela cheia
// de um jeito que SOBREVIVE a aba ir pro background — fetch normal morre quando o
// Chrome Android troca de aba e descarta a antiga, e o comando nunca chegava no PC
// (era a causa de "não abre a TV sozinho"). keepalive + sendBeacon resolvem isso.
// (2) só então abre o /controle aqui, dentro do mesmo gesto (não cair em pop-up blocker).
function abrirControleRemoto(ev){
  if(ev) ev.preventDefault();
  try{
    fetch('/abrir-tela-pc',{method:'POST',keepalive:true})
      .then(r=>r.json())
      .then(d=>{ showToast(d&&d.ok ? '🖼️ Visualização aberta no PC'
                                   : '⚠️ TV não abriu — o Fooocus está no ar?',
                          d&&d.ok ? '#22c55e' : '#ef4444'); })
      .catch(()=>{ try{navigator.sendBeacon('/abrir-tela-pc');}catch(e){} });
  }catch(e){ try{navigator.sendBeacon('/abrir-tela-pc');}catch(_){} }
  window.open(URL_FOO + '/controle','_blank');
  return false;
}

// restaurar último estado salvo ANTES de buscar na rede (elimina flash "verificando...")
try{const c=JSON.parse(localStorage.getItem('_st'));if(c)render(c);}catch{}

// buscar estado real (atualiza assim que a rede responder)
(async()=>{render(await fetchStatus());})();
setInterval(async()=>{if(pollTimer)return;render(await fetchStatus());},6000);

// refresh imediato ao voltar do background (celular)
document.addEventListener('visibilitychange',()=>{
  if(document.visibilityState==='visible') fetchStatus().then(render);
});

// impede sair por engano (toque no "voltar" fecha o Chrome e perde tudo).
// O navegador mostra "Sair do site?" — confirmacao antes de fechar/voltar.
window.addEventListener('beforeunload',e=>{e.preventDefault();e.returnValue='';});
</script>
</body>
</html>
""".replace("FOOOCUS_URL", FOOOCUS_URL)


# ── HTTP handler ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/status':
            self._json(get_status())
        elif self.path == '/logs':
            self._json({'lines': list(_log_buffer)})
        elif self.path == '/flog':
            try:
                with open(FOOOCUS_CONSOLE_LOG, 'r', encoding='utf-8', errors='replace') as f:
                    data = f.read()[-10000:]
            except Exception:
                data = '(sem log do Fooocus ainda — inicie o Fooocus primeiro)'
            body = data.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', len(body))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
        elif self.path == '/manifest.json':
            body = MANIFEST_JSON.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(body))
            # no-cache = browser sempre revalida → fix de código SEMPRE chega ao cliente.
            # Sensação instantânea no reload vem do localStorage (estado), não de cachear o código.
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self):
        if self.path == '/start':
            self._json({'ok': True, 'started': start_fooocus()})
        elif self.path == '/stop':
            stop_fooocus()
            self._json({'ok': True})
        elif self.path == '/abrir-tela-pc':
            self._json({'ok': abrir_tela_no_pc()})
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == '__main__':
    threading.Thread(target=_watch_inatividade, daemon=True).start()
    log.info(f'Watchdog de inatividade ligado (limite {INATIVIDADE_LIMITE}s)')
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    log.info(f'Ouvindo em 0.0.0.0:{PORT}')
    try:
        server.serve_forever()
    except Exception as e:
        log.critical(f'Servidor encerrado com erro: {e}')
