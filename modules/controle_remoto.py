# -*- coding: utf-8 -*-
"""
Controle Remoto Split do RuinedFooocus  (add-on Atila/Claude, 24/06/2026)
=========================================================================
Separa a operacao em dois aparelhos:
  /controle  -> CELULAR: todos os controles (prompt, negativo, checkpoint, loras,
                estilo, resolucao, nº de imagens, seed) + PRESETS de prompt. SEM imagem.
  /tela      -> PC/TV: SO a imagem gerada, em tela cheia, atualizando sozinha.

Roda DENTRO do processo do Fooocus (montado em shared.server_app, o FastAPI do Gradio),
entao a geracao chama worker.add_task direto (mesmo mecanismo da API oficial em modules/api.py)
e a imagem vem de shared.state["last_image"] direto. Sem protocolo HTTP entre processos.

NAO altera nada do resto do Fooocus. Se este modulo falhar, o try/except no webui.py
garante que o Fooocus sobe normalmente mesmo assim.
"""
import json
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse

import shared
from shared import settings, path_manager
import modules.async_worker as worker

# arquivo de presets (do nosso lado, nao mexe nos "presets" nativos do Fooocus)
PRESETS_FILE = Path(__file__).parent.parent / "settings" / "cr_presets.json"

# preset negativo semente pedido pelo Atila
PRESET_NEG_SEED = ("((simple background)), ((static background)), sketch, drawing, grayscale, "
                   "monochrome, unpainted, artist name, gay, lesbian, futanari.")


def _presets_default():
    return {
        "positivos": [],
        "negativos": [{"nome": "Padrao (limpo)", "texto": PRESET_NEG_SEED}],
    }


def _load_presets():
    if PRESETS_FILE.exists():
        try:
            return json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _presets_default()


def _save_presets(p):
    PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PRESETS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PRESETS_FILE)   # escrita atomica write-then-rename (evita arquivo corrompido)


def _ensure_seed():
    # cria o arquivo com o preset semente no primeiro uso
    if not PRESETS_FILE.exists():
        _save_presets(_presets_default())


# ── listar checkpoints / loras / estilos pros dropdowns ─────────────────────────
def _listar(folder_key):
    fp = path_manager.model_paths.get(folder_key)
    folders = fp if isinstance(fp, list) else [fp]
    nomes, seen = [], set()
    for f in folders:
        try:
            for n in path_manager.get_model_filenames(f):
                if n not in seen:
                    seen.add(n)
                    nomes.append(n)
        except Exception:
            pass
    return nomes


def _opcoes():
    try:
        checkpoints = _listar("modelfile_path")
    except Exception:
        checkpoints = []
    try:
        loras = _listar("lorafile_path")
    except Exception:
        loras = []
    try:
        from modules.sdxl_styles import styles as _st
        estilos = list(_st.keys()) if isinstance(_st, dict) else list(_st)
    except Exception:
        estilos = []
    d = settings.default_settings
    return {
        "checkpoints": checkpoints,
        "loras": loras,
        "estilos": estilos,
        "default": {
            "checkpoint": d.get("base_model"),
            "performance": d.get("performance"),
            "resolution": d.get("resolution"),
            "style": d.get("style"),
        },
    }


# ── geracao: monta o task igual a api.py oficial e submete (fire-and-forget) ─────
def _submeter(payload):
    d = settings.default_settings
    loras_in = payload.get("loras") or []
    loras = []
    for i in range(5):
        item = loras_in[i] if i < len(loras_in) else {}
        m = (item or {}).get("model") or d.get(f"lora_{i+1}_model", "None")
        w = (item or {}).get("weight", d.get(f"lora_{i+1}_weight", 1.0))
        loras.append(("", f"{w} - {m}"))

    tmp = {
        "task_type": "api_process",
        "prompt": payload.get("prompt", "") or "",
        "negative": payload.get("negative", "") or "",
        "loras": loras,
        "style_selection": payload.get("style") if payload.get("style") is not None else d["style"],
        "seed": int(payload.get("seed", -1) or -1),
        "base_model_name": payload.get("checkpoint") or d["base_model"],
        "performance_selection": payload.get("performance") or d["performance"],
        "aspect_ratios_selection": payload.get("resolution") or d["resolution"],
        "cn_selection": None,
        "cn_type": None,
        "image_number": int(payload.get("image_number", 1) or 1),
    }
    return worker.add_task(tmp.copy())


def _ultima():
    p = shared.state.get("last_image") if isinstance(shared.state, dict) else None
    if not p:
        return {"tem": False}
    try:
        mtime = Path(p).stat().st_mtime
    except Exception:
        mtime = 0
    return {"tem": True, "id": mtime}


# ── endpoints HTTP ──────────────────────────────────────────────────────────────
async def _ep_controle(request: Request):
    return HTMLResponse(HTML_CONTROLE)


async def _ep_tela(request: Request):
    return HTMLResponse(HTML_TELA)


async def _ep_opcoes(request: Request):
    return JSONResponse(_opcoes())


async def _ep_presets_get(request: Request):
    return JSONResponse(_load_presets())


async def _ep_presets_post(request: Request):
    # body: {"tipo":"positivos"|"negativos","acao":"add"|"del","nome":...,"texto":...}
    try:
        b = await request.json()
        tipo = b.get("tipo")
        if tipo not in ("positivos", "negativos"):
            return JSONResponse({"ok": False, "erro": "tipo invalido"}, status_code=400)
        p = _load_presets()
        lista = p.setdefault(tipo, [])
        if b.get("acao") == "del":
            p[tipo] = [x for x in lista if x.get("nome") != b.get("nome")]
        else:
            nome = (b.get("nome") or "").strip() or "Sem nome"
            texto = b.get("texto") or ""
            lista[:] = [x for x in lista if x.get("nome") != nome]  # substitui se mesmo nome
            lista.append({"nome": nome, "texto": texto})
        _save_presets(p)
        return JSONResponse({"ok": True, "presets": p})
    except Exception as e:
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)


async def _ep_gerar(request: Request):
    try:
        b = await request.json()
        task_id = _submeter(b)
        return JSONResponse({"ok": True, "task_id": str(task_id)})
    except Exception as e:
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)


async def _ep_ultima(request: Request):
    return JSONResponse(_ultima())


async def _ep_imagem(request: Request):
    p = shared.state.get("last_image") if isinstance(shared.state, dict) else None
    if p and Path(p).exists():
        return FileResponse(p)
    return PlainTextResponse("sem imagem ainda", status_code=404)


def montar(app):
    """Registra as rotas no FastAPI do Gradio. Chamado pelo webui.py apos o launch."""
    from fastapi.routing import APIRoute
    _ensure_seed()
    rotas = [
        ("/controle", _ep_controle, ["GET"]),
        ("/tela", _ep_tela, ["GET"]),
        ("/cr/opcoes", _ep_opcoes, ["GET"]),
        ("/cr/presets", _ep_presets_get, ["GET"]),
        ("/cr/presets", _ep_presets_post, ["POST"]),
        ("/cr/gerar", _ep_gerar, ["POST"]),
        ("/cr/ultima", _ep_ultima, ["GET"]),
        ("/cr/imagem", _ep_imagem, ["GET"]),
    ]
    # insere na FRENTE pra ter precedencia sobre qualquer catch-all do Gradio
    for path, ep, methods in rotas:
        app.router.routes.insert(0, APIRoute(path, ep, methods=methods))
    print("[controle_remoto] rotas montadas: /controle (celular) e /tela (TV)")


# ── PAGINA DE CONTROLE (celular) ────────────────────────────────────────────────
HTML_CONTROLE = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Fooocus — Controle</title>
<meta name="theme-color" content="#0f0f13">
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:#0f0f13;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  padding:16px 14px 120px;max-width:560px;margin:0 auto}
h1{font-size:16px;font-weight:700;letter-spacing:1px;color:#fff;margin-bottom:4px;text-transform:uppercase}
h1 span{color:#a78bfa}
.hint{font-size:12px;color:#6b7280;margin-bottom:16px;line-height:1.4}
label{display:block;font-size:12px;color:#9ca3af;text-transform:uppercase;letter-spacing:.5px;margin:14px 0 6px}
textarea,select,input[type=number],input[type=text]{
  width:100%;background:#1a1a24;color:#e8e8e8;border:1.5px solid #2a2a3a;border-radius:10px;
  padding:12px;font-size:15px;font-family:inherit}
textarea{min-height:80px;resize:vertical}
.row{display:flex;gap:8px;align-items:center}
.row select{flex:1}
.row input[type=number]{width:90px}
.preset-row{display:flex;gap:8px;margin-top:6px}
.preset-row select{flex:1}
.mini{padding:10px 12px;font-size:13px;border:none;border-radius:10px;background:#252533;color:#cbd5e1;cursor:pointer}
.mini:active{opacity:.7}
.lora{display:flex;gap:8px;margin-bottom:8px}
.lora select{flex:1}
.lora input{width:78px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.bar{position:fixed;left:0;right:0;bottom:0;background:#13131aee;backdrop-filter:blur(8px);
  padding:14px;border-top:1px solid #2a2a3a;display:flex;gap:10px;align-items:center;max-width:560px;margin:0 auto}
.gerar{flex:1;padding:18px;border:none;border-radius:14px;background:#a78bfa;color:#0f0f13;
  font-size:17px;font-weight:700;cursor:pointer}
.gerar:active{opacity:.8}.gerar:disabled{opacity:.4}
.st{font-size:12px;color:#9ca3af;min-width:96px;text-align:right}
a.tv{color:#a78bfa;font-size:13px;text-decoration:none;border:1px solid #a78bfa;border-radius:8px;padding:8px 10px;display:inline-block;margin-top:8px}
details{margin-top:14px;border:1px solid #2a2a3a;border-radius:10px;padding:10px 12px}
summary{font-size:13px;color:#9ca3af;cursor:pointer}
</style>
</head>
<body>
<h1>Ruined<span>Fooocus</span> — Controle</h1>
<div class="hint">Escreva e ajuste tudo por aqui. A imagem aparece na TV/PC:
<a class="tv" id="tvlink" href="/tela" target="_blank">abrir tela cheia no PC →</a></div>

<label>Prompt</label>
<textarea id="prompt" placeholder="descreva a imagem..."></textarea>
<div class="preset-row">
  <select id="presetPos"><option value="">— presets positivos —</option></select>
  <button class="mini" onclick="aplicar('pos')">Aplicar</button>
  <button class="mini" onclick="salvar('positivos','prompt')">Salvar</button>
</div>

<label>Prompt negativo</label>
<textarea id="negative" placeholder="o que NAO quer na imagem..."></textarea>
<div class="preset-row">
  <select id="presetNeg"><option value="">— presets negativos —</option></select>
  <button class="mini" onclick="aplicar('neg')">Aplicar</button>
  <button class="mini" onclick="salvar('negativos','negative')">Salvar</button>
</div>

<label>Checkpoint (modelo)</label>
<select id="checkpoint"></select>

<details>
<summary>LoRAs (opcional)</summary>
<div id="loras"></div>
</details>

<details>
<summary>Estilos / avancado (opcional)</summary>
<label>Estilos</label>
<select id="style" multiple size="4"></select>
<div class="grid2">
  <div><label>Resolucao</label><input type="text" id="resolution"></div>
  <div><label>Performance</label><input type="text" id="performance"></div>
</div>
<div class="grid2">
  <div><label>Nº de imagens</label><input type="number" id="image_number" value="1" min="1" max="32"></div>
  <div><label>Seed (-1 = aleatorio)</label><input type="number" id="seed" value="-1"></div>
</div>
</details>

<div class="bar">
  <button class="gerar" id="bGerar" onclick="gerar()">Gerar</button>
  <div class="st" id="st"></div>
</div>
<script>
const $=id=>document.getElementById(id);
let PRESETS={positivos:[],negativos:[]};
let lastId=null, watching=false;

function opt(v,sel){const o=document.createElement('option');o.value=v;o.textContent=v;if(sel)o.selected=true;return o;}

async function init(){
  const o=await fetch('/cr/opcoes').then(r=>r.json());
  o.checkpoints.forEach(c=>$('checkpoint').appendChild(opt(c,c===o.default.checkpoint)));
  const lc=$('loras');
  for(let i=0;i<5;i++){
    const div=document.createElement('div');div.className='lora';
    const s=document.createElement('select');s.appendChild(opt('None',true));
    o.loras.forEach(l=>s.appendChild(opt(l,false)));s.dataset.lora=i;
    const w=document.createElement('input');w.type='number';w.step='0.1';w.value='1';w.dataset.w=i;
    div.appendChild(s);div.appendChild(w);lc.appendChild(div);
  }
  (o.estilos||[]).forEach(e=>$('style').appendChild(opt(e,false)));
  if(o.default.resolution)$('resolution').value=o.default.resolution;
  if(o.default.performance)$('performance').value=o.default.performance;
  await carregarPresets();
  const u=await fetch('/cr/ultima').then(r=>r.json());lastId=u.tem?u.id:null;
}

async function carregarPresets(){
  PRESETS=await fetch('/cr/presets').then(r=>r.json());
  for(const [sel,key] of [['presetPos','positivos'],['presetNeg','negativos']]){
    const el=$(sel);el.length=1;
    (PRESETS[key]||[]).forEach(p=>el.appendChild(opt(p.nome,false)));
  }
}
function aplicar(t){
  const sel=t==='pos'?$('presetPos'):$('presetNeg');
  const key=t==='pos'?'positivos':'negativos';
  const campo=t==='pos'?'prompt':'negative';
  const p=(PRESETS[key]||[]).find(x=>x.nome===sel.value);
  if(p)$(campo).value=p.texto;
}
async function salvar(tipo,campo){
  const nome=prompt('Nome do preset:');if(!nome)return;
  const texto=$(campo).value;
  await fetch('/cr/presets',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({tipo,acao:'add',nome,texto})});
  await carregarPresets();setSt('preset salvo');
}
function setSt(m){$('st').textContent=m;}

function coletarLoras(){
  const out=[];document.querySelectorAll('#loras .lora').forEach(div=>{
    const m=div.querySelector('select').value;const w=parseFloat(div.querySelector('input').value)||1;
    out.push({model:m,weight:w});});
  return out;
}
async function gerar(){
  $('bGerar').disabled=true;setSt('enviando...');
  const styleSel=[...$('style').selectedOptions].map(o=>o.value);
  const payload={
    prompt:$('prompt').value,negative:$('negative').value,
    checkpoint:$('checkpoint').value,loras:coletarLoras(),
    style: styleSel.length?styleSel:null,
    resolution:$('resolution').value||null,performance:$('performance').value||null,
    image_number:parseInt($('image_number').value)||1,seed:parseInt($('seed').value)||-1
  };
  try{
    const r=await fetch('/cr/gerar',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)}).then(r=>r.json());
    if(!r.ok){setSt('erro: '+(r.erro||'?'));$('bGerar').disabled=false;return;}
    setSt('gerando... veja na TV');watchNova();
  }catch(e){setSt('erro de conexao');$('bGerar').disabled=false;}
}
function watchNova(){
  if(watching)return;watching=true;
  const iv=setInterval(async()=>{
    try{const u=await fetch('/cr/ultima').then(r=>r.json());
      if(u.tem&&u.id!==lastId){lastId=u.id;setSt('imagem pronta na TV ✓');
        $('bGerar').disabled=false;watching=false;clearInterval(iv);}
    }catch{}
  },1500);
  // libera o botao depois de no max 5min mesmo sem deteccao
  setTimeout(()=>{$('bGerar').disabled=false;watching=false;clearInterval(iv);},300000);
}
init();
</script>
</body>
</html>"""


# ── PAGINA DA TV (PC) — so a imagem, tela cheia ─────────────────────────────────
HTML_TELA = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fooocus — Tela</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;background:#000;overflow:hidden}
#wrap{height:100vh;width:100vw;display:flex;align-items:center;justify-content:center}
#img{max-height:100vh;max-width:100vw;object-fit:contain;display:none}
#ph{color:#333;font-family:-apple-system,'Segoe UI',sans-serif;font-size:20px;letter-spacing:2px;text-transform:uppercase}
#dot{position:fixed;top:14px;right:16px;width:10px;height:10px;border-radius:50%;background:#222;transition:background .3s}
#dot.live{background:#22c55e}
</style>
</head>
<body>
<div id="wrap">
  <img id="img" alt="">
  <div id="ph">aguardando imagem…</div>
</div>
<div id="dot" title="atualizando"></div>
<script>
const img=document.getElementById('img'),ph=document.getElementById('ph'),dot=document.getElementById('dot');
let lastId=null;
async function tick(){
  try{
    const u=await fetch('/cr/ultima',{cache:'no-store'}).then(r=>r.json());
    dot.classList.add('live');setTimeout(()=>dot.classList.remove('live'),400);
    if(u.tem&&u.id!==lastId){
      lastId=u.id;
      const novo=new Image();
      novo.onload=()=>{img.src=novo.src;img.style.display='block';ph.style.display='none';};
      novo.src='/cr/imagem?t='+encodeURIComponent(u.id);
    }
  }catch(e){}
}
setInterval(tick,1500);tick();
// mantem a tela acordada se o browser suportar
if('wakeLock'in navigator){const req=()=>navigator.wakeLock.request('screen').catch(()=>{});req();
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')req();});}
</script>
</body>
</html>"""
