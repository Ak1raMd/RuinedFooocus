# -*- coding: utf-8 -*-
"""
Controle Remoto Split do RuinedFooocus  (add-on Atila/Claude, 24/06/2026)
=========================================================================
v2 — melhorias: dropdowns nativos, thumbnails, preview em tempo real,
     botao parar/regenerar, barras expansiveis, icones de proporcao.
"""
import json
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse

import shared
from shared import settings, path_manager
import modules.async_worker as worker

PRESETS_FILE = Path(__file__).parent.parent / "settings" / "cr_presets.json"

PRESET_NEG_SEED = ("((simple background)), ((static background)), sketch, drawing, grayscale, "
                   "monochrome, unpainted, artist name, gay, lesbian, futanari.")


def _presets_default():
    return {
        "positivos": [],
        "negativos": [{"nome": "Preset 1", "texto": PRESET_NEG_SEED}],
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
    tmp.replace(PRESETS_FILE)


def _ensure_seed():
    if not PRESETS_FILE.exists():
        _save_presets(_presets_default())


# ── listar checkpoints / loras / estilos ────────────────────────────────────────
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


def _find_thumbnail(cache_subdir, model_name):
    cache_base = path_manager.model_paths.get("cache_path")
    if not cache_base:
        return None
    base = Path(cache_base) / cache_subdir / Path(model_name).name
    for ext in (".jpeg", ".jpg", ".png", ".gif"):
        candidate = base.with_suffix(ext)
        if candidate.is_file():
            return str(candidate)
    return None


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

    perf_names = list(shared.performance_settings.performance_options.keys())
    res_map = shared.resolution_settings.aspect_ratios
    resolutions = []
    for label, (w, h) in res_map.items():
        resolutions.append({"label": label, "w": w, "h": h})

    ckpt_thumbs = {}
    for c in checkpoints:
        if _find_thumbnail("checkpoints", c):
            ckpt_thumbs[c] = True
    lora_thumbs = {}
    for l in loras:
        if _find_thumbnail("loras", l):
            lora_thumbs[l] = True

    d = settings.default_settings
    return {
        "checkpoints": checkpoints,
        "loras": loras,
        "estilos": estilos,
        "performances": perf_names,
        "resolutions": resolutions,
        "ckpt_thumbs": ckpt_thumbs,
        "lora_thumbs": lora_thumbs,
        "default": {
            "checkpoint": d.get("base_model"),
            "performance": "SD3",
            "resolution": d.get("resolution"),
            "style": d.get("style"),
        },
    }


# ── geracao ─────────────────────────────────────────────────────────────────────
_current_task_id = None

def _submeter(payload):
    global _current_task_id
    d = settings.default_settings
    loras_in = payload.get("loras") or []
    loras = []
    for i in range(5):
        item = loras_in[i] if i < len(loras_in) else {}
        m = (item or {}).get("model") or d.get(f"lora_{i+1}_model", "None")
        w = (item or {}).get("weight", d.get(f"lora_{i+1}_weight", 1.0))
        loras.append(("", f"{w} - {m}"))

    tmp = {
        "task_type": "process",
        "prompt": payload.get("prompt", "") or "",
        "negative": payload.get("negative", "") or "",
        "loras": loras,
        "style_selection": payload.get("style") if payload.get("style") is not None else d["style"],
        "seed": int(payload.get("seed", -1) or -1),
        "base_model_name": payload.get("checkpoint") or d["base_model"],
        "performance_selection": payload.get("performance") or "SD3",
        "aspect_ratios_selection": payload.get("resolution") or d["resolution"],
        "cn_selection": None,
        "cn_type": None,
        "image_number": int(payload.get("image_number", 1) or 1),
    }
    _current_task_id = worker.add_task(tmp.copy())
    return _current_task_id


def _ultima():
    p = shared.state.get("last_image") if isinstance(shared.state, dict) else None
    if not p:
        return {"tem": False}
    try:
        mtime = Path(p).stat().st_mtime
    except Exception:
        mtime = 0
    return {"tem": True, "id": mtime}


def _preview_info():
    preview_path = path_manager.model_paths.get("temp_preview_path")
    if not preview_path or not Path(preview_path).exists():
        return {"tem": False}
    try:
        mtime = Path(preview_path).stat().st_mtime
    except Exception:
        return {"tem": False}
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
            lista[:] = [x for x in lista if x.get("nome") != nome]
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


async def _ep_parar(request: Request):
    worker.interrupt_ruined_processing = True
    return JSONResponse({"ok": True})


async def _ep_ultima(request: Request):
    return JSONResponse(_ultima())


async def _ep_preview(request: Request):
    return JSONResponse(_preview_info())


async def _ep_preview_img(request: Request):
    preview_path = path_manager.model_paths.get("temp_preview_path")
    if preview_path and Path(preview_path).exists():
        return FileResponse(preview_path, media_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})
    return PlainTextResponse("sem preview", status_code=404)


async def _ep_imagem(request: Request):
    p = shared.state.get("last_image") if isinstance(shared.state, dict) else None
    if p and Path(p).exists():
        return FileResponse(p)
    return PlainTextResponse("sem imagem ainda", status_code=404)


async def _ep_thumb(request: Request):
    tipo = request.path_params.get("tipo", "")
    nome = request.path_params.get("nome", "")
    subdir = {"ckpt": "checkpoints", "lora": "loras"}.get(tipo)
    if not subdir:
        return PlainTextResponse("tipo invalido", status_code=400)
    path = _find_thumbnail(subdir, nome)
    if path and Path(path).exists():
        return FileResponse(path)
    return PlainTextResponse("sem thumb", status_code=404)


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
        ("/cr/parar", _ep_parar, ["POST"]),
        ("/cr/ultima", _ep_ultima, ["GET"]),
        ("/cr/preview", _ep_preview, ["GET"]),
        ("/cr/preview_img", _ep_preview_img, ["GET"]),
        ("/cr/imagem", _ep_imagem, ["GET"]),
        ("/cr/thumb/{tipo}/{nome:path}", _ep_thumb, ["GET"]),
    ]
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
  padding:16px 14px 130px;max-width:560px;margin:0 auto}
h1{font-size:16px;font-weight:700;letter-spacing:1px;color:#fff;margin-bottom:4px;text-transform:uppercase}
h1 span{color:#a78bfa}
.hint{font-size:12px;color:#6b7280;margin-bottom:16px;line-height:1.4}
label{display:block;font-size:12px;color:#9ca3af;text-transform:uppercase;letter-spacing:.5px;margin:14px 0 6px}
textarea,select,input[type=number],input[type=text]{
  width:100%;background:#1a1a24;color:#e8e8e8;border:1.5px solid #2a2a3a;border-radius:10px;
  padding:12px;font-size:15px;font-family:inherit}
textarea{min-height:80px;resize:vertical}
.preset-row{display:flex;gap:8px;margin-top:6px}
.preset-row select{flex:1;font-size:13px;padding:8px}
.mini{padding:10px 12px;font-size:13px;border:none;border-radius:10px;background:#252533;color:#cbd5e1;cursor:pointer}
.mini:active{opacity:.7}

/* barras expansiveis */
.ebar{margin-top:14px;border:1.5px solid #2a2a3a;border-radius:12px;overflow:hidden}
.ebar-head{display:flex;align-items:center;gap:10px;padding:12px 14px;cursor:pointer;
  background:#15151e;user-select:none}
.ebar-head .arrow{color:#555;font-size:14px;transition:transform .2s}
.ebar.open .arrow{transform:rotate(90deg)}
.ebar-head .elabel{font-size:12px;color:#9ca3af;text-transform:uppercase;letter-spacing:.5px;flex-shrink:0}
.ebar-head .evalue{font-size:13px;color:#e0e0e0;flex:1;text-align:right;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.ebar-head .ethumb{width:36px;height:36px;border-radius:6px;object-fit:cover;flex-shrink:0}
.ebar-body{display:none;padding:10px 12px;max-height:50vh;overflow-y:auto}
.ebar.open .ebar-body{display:block}

/* model cards (checkpoint + lora) */
.model-card{display:flex;align-items:center;gap:10px;background:#1a1a24;border:1.5px solid #2a2a3a;
  border-radius:10px;padding:8px 12px;cursor:pointer;transition:border-color .15s;margin-bottom:6px}
.model-card.sel{border-color:#a78bfa;background:#1f1a2e}
.model-card img{width:48px;height:48px;border-radius:6px;object-fit:cover;flex-shrink:0;background:#252533}
.model-card .name{font-size:13px;line-height:1.3;word-break:break-word;flex:1}
.model-card .noimg{width:48px;height:48px;border-radius:6px;background:#252533;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;font-size:20px;color:#444}

/* lora slot */
.lora-slot{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.lora-slot .lpick{flex:1;display:flex;align-items:center;gap:8px;background:#1a1a24;border:1.5px solid #2a2a3a;
  border-radius:10px;padding:6px 10px;overflow:hidden;cursor:pointer}
.lora-slot .lpick.active{border-color:#a78bfa}
.lora-slot .lpick img{width:36px;height:36px;border-radius:5px;object-fit:cover;flex-shrink:0}
.lora-slot .lpick .lname{font-size:12px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#bbb}
.lora-slot input{width:58px;background:#1a1a24;color:#e8e8e8;border:1.5px solid #2a2a3a;border-radius:10px;
  padding:8px 4px;font-size:13px;text-align:center}

/* lora picker overlay */
.lpicker-overlay{display:none;position:fixed;inset:0;background:#0008;z-index:100}
.lpicker-overlay.show{display:flex;flex-direction:column;align-items:center;padding:40px 10px 10px}
.lpicker-box{background:#15151e;border-radius:14px;width:100%;max-width:520px;max-height:80vh;
  overflow-y:auto;padding:12px}
.lpicker-box .model-card{margin-bottom:6px}

/* resolution cards */
.res-card{display:flex;align-items:center;gap:10px;background:#1a1a24;border:1.5px solid #2a2a3a;
  border-radius:10px;padding:10px 14px;cursor:pointer;margin-bottom:6px;transition:border-color .15s}
.res-card.sel{border-color:#a78bfa;background:#1f1a2e}
.res-icon{flex-shrink:0;display:flex;align-items:center;justify-content:center;width:48px;height:48px}
.res-icon .box{border:2px solid #666;border-radius:3px;transition:border-color .15s}
.res-card.sel .res-icon .box{border-color:#a78bfa}
.res-label{font-size:14px;flex:1}.res-dim{font-size:11px;color:#666}

/* performance cards */
.perf-card{display:flex;align-items:center;gap:10px;background:#1a1a24;border:1.5px solid #2a2a3a;
  border-radius:10px;padding:12px 14px;cursor:pointer;margin-bottom:6px;transition:border-color .15s}
.perf-card.sel{border-color:#a78bfa;background:#1f1a2e}
.perf-name{font-size:14px;font-weight:600;flex:1}

.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.bar{position:fixed;left:0;right:0;bottom:0;background:#13131aee;backdrop-filter:blur(8px);
  padding:14px;border-top:1px solid #2a2a3a;display:flex;gap:10px;align-items:center;max-width:560px;margin:0 auto}
.gerar{flex:1;padding:18px;border:none;border-radius:14px;background:#a78bfa;color:#0f0f13;
  font-size:17px;font-weight:700;cursor:pointer}
.gerar:active{opacity:.8}.gerar:disabled{opacity:.4}
.parar{flex:1;padding:18px;border:none;border-radius:14px;background:#ef4444;color:#fff;
  font-size:17px;font-weight:700;cursor:pointer;display:none}
.parar:active{opacity:.8}
.regen{flex:1;padding:18px;border:none;border-radius:14px;background:#22c55e;color:#0f0f13;
  font-size:17px;font-weight:700;cursor:pointer;display:none}
.regen:active{opacity:.8}
.st{font-size:12px;color:#9ca3af;min-width:80px;text-align:right}
a.tv{color:#a78bfa;font-size:13px;text-decoration:none;border:1px solid #a78bfa;border-radius:8px;
  padding:8px 10px;display:inline-block;margin-top:8px}
</style>
</head>
<body>
<h1>Ruined<span>Fooocus</span> — Controle</h1>
<div class="hint">Escreva e ajuste tudo por aqui. A imagem aparece na TV/PC:
<a class="tv" href="/tela" target="_blank">abrir tela cheia no PC →</a></div>

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

<!-- Checkpoint bar -->
<div class="ebar" id="ckptBar">
  <div class="ebar-head" onclick="toggle('ckptBar')">
    <span class="arrow">▸</span><span class="elabel">Checkpoint</span>
    <img class="ethumb" id="ckptThumb" style="display:none">
    <span class="evalue" id="ckptVal">...</span>
  </div>
  <div class="ebar-body" id="ckptBody"></div>
</div>

<!-- Lora bar -->
<div class="ebar" id="loraBar">
  <div class="ebar-head" onclick="toggle('loraBar')">
    <span class="arrow">▸</span><span class="elabel">LoRAs</span>
    <span class="evalue" id="loraVal">nenhuma</span>
  </div>
  <div class="ebar-body" id="loraBody"></div>
</div>

<!-- Resolution bar -->
<div class="ebar" id="resBar">
  <div class="ebar-head" onclick="toggle('resBar')">
    <span class="arrow">▸</span><span class="elabel">Resolução</span>
    <span class="evalue" id="resVal">...</span>
  </div>
  <div class="ebar-body" id="resBody"></div>
</div>

<!-- Performance bar -->
<div class="ebar" id="perfBar">
  <div class="ebar-head" onclick="toggle('perfBar')">
    <span class="arrow">▸</span><span class="elabel">Performance</span>
    <span class="evalue" id="perfVal">...</span>
  </div>
  <div class="ebar-body" id="perfBody"></div>
</div>

<!-- Estilos + extra -->
<div class="ebar" id="extraBar">
  <div class="ebar-head" onclick="toggle('extraBar')">
    <span class="arrow">▸</span><span class="elabel">Estilos / Avançado</span>
    <span class="evalue">&nbsp;</span>
  </div>
  <div class="ebar-body">
    <label>Estilos</label>
    <select id="style" multiple size="4"></select>
    <div class="grid2">
      <div><label>Nº de imagens</label><input type="number" id="image_number" value="1" min="1" max="32"></div>
      <div><label>Seed (-1 = aleatório)</label><input type="number" id="seed" value="-1"></div>
    </div>
  </div>
</div>

<!-- Lora picker overlay -->
<div class="lpicker-overlay" id="loraPicker" onclick="closeLoraPicker(event)">
  <div class="lpicker-box" id="loraPickerBox"></div>
</div>

<div class="bar">
  <button class="gerar" id="bGerar" onclick="gerar()">Gerar</button>
  <button class="parar" id="bParar" onclick="parar()">Parar</button>
  <button class="regen" id="bRegen" onclick="gerar()">Regenerar</button>
  <div class="st" id="st"></div>
</div>
<script>
const $=id=>document.getElementById(id);
let PRESETS={positivos:[],negativos:[]};
let lastId=null,watching=false,generating=false,wasStopped=false;
let selectedCkpt='',selectedRes='',selectedPerf='SD3';
let allLoras=[],loraThumbMap={},ckptThumbMap={};
let loraSlotTarget=-1;

function opt(v,sel){const o=document.createElement('option');o.value=v;o.textContent=v;if(sel)o.selected=true;return o;}
function thumbUrl(tipo,nome){return '/cr/thumb/'+tipo+'/'+encodeURIComponent(nome);}
function shortName(n){return n.replace(/\.safetensors$/i,'').replace(/_/g,' ');}
function toggle(id){$(id).classList.toggle('open');}

/* ── Checkpoint cards ── */
function buildCkptCards(checkpoints,thumbs,def){
  const wrap=$('ckptBody');wrap.innerHTML='';
  selectedCkpt=def||checkpoints[0]||'';ckptThumbMap=thumbs;
  checkpoints.forEach(c=>{
    const card=document.createElement('div');card.className='model-card'+(c===selectedCkpt?' sel':'');
    card.dataset.val=c;
    if(thumbs[c]){const img=document.createElement('img');img.src=thumbUrl('ckpt',c);img.loading='lazy';card.appendChild(img);}
    else{const ph=document.createElement('div');ph.className='noimg';ph.textContent='🎨';card.appendChild(ph);}
    const nm=document.createElement('div');nm.className='name';nm.textContent=shortName(c);card.appendChild(nm);
    card.onclick=()=>{
      wrap.querySelectorAll('.model-card').forEach(x=>x.classList.remove('sel'));
      card.classList.add('sel');selectedCkpt=c;updateCkptHead();$('ckptBar').classList.remove('open');
    };
    wrap.appendChild(card);
  });
  updateCkptHead();
}
function updateCkptHead(){
  $('ckptVal').textContent=shortName(selectedCkpt);
  const th=$('ckptThumb');
  if(ckptThumbMap[selectedCkpt]){th.src=thumbUrl('ckpt',selectedCkpt);th.style.display='block';}
  else{th.style.display='none';}
}

/* ── Lora slots + picker ── */
function buildLoraSlots(loras,thumbs){
  allLoras=loras;loraThumbMap=thumbs;
  const wrap=$('loraBody');wrap.innerHTML='';
  for(let i=0;i<5;i++){
    const slot=document.createElement('div');slot.className='lora-slot';slot.dataset.idx=i;
    const pick=document.createElement('div');pick.className='lpick';
    const img=document.createElement('img');img.style.display='none';img.dataset.idx=i;pick.appendChild(img);
    const lname=document.createElement('div');lname.className='lname';lname.textContent='None';lname.dataset.idx=i;
    pick.appendChild(lname);
    pick.onclick=()=>{openLoraPicker(i);};
    slot.appendChild(pick);
    const w=document.createElement('input');w.type='number';w.step='0.1';w.value='1';w.dataset.w=i;
    slot.appendChild(w);wrap.appendChild(slot);
  }
}
function openLoraPicker(idx){
  loraSlotTarget=idx;
  const box=$('loraPickerBox');box.innerHTML='';
  const none=document.createElement('div');none.className='model-card';none.innerHTML='<div class="noimg">✕</div><div class="name">Nenhuma</div>';
  none.onclick=()=>{selectLora(idx,'None');};box.appendChild(none);
  allLoras.forEach(l=>{
    const card=document.createElement('div');card.className='model-card';
    if(loraThumbMap[l]){const img=document.createElement('img');img.src=thumbUrl('lora',l);img.loading='lazy';card.appendChild(img);}
    else{const ph=document.createElement('div');ph.className='noimg';ph.textContent='🎨';card.appendChild(ph);}
    const nm=document.createElement('div');nm.className='name';nm.textContent=shortName(l);card.appendChild(nm);
    card.onclick=()=>{selectLora(idx,l);};
    box.appendChild(card);
  });
  $('loraPicker').classList.add('show');
}
function closeLoraPicker(e){if(e.target===$('loraPicker'))$('loraPicker').classList.remove('show');}
function selectLora(idx,val){
  const slot=document.querySelectorAll('.lora-slot')[idx];
  const img=slot.querySelector('img');const lname=slot.querySelector('.lname');const pick=slot.querySelector('.lpick');
  if(val==='None'){img.style.display='none';lname.textContent='None';pick.classList.remove('active');pick.dataset.val='None';}
  else{
    if(loraThumbMap[val]){img.src=thumbUrl('lora',val);img.style.display='block';}else{img.style.display='none';}
    lname.textContent=shortName(val);pick.classList.add('active');pick.dataset.val=val;
  }
  $('loraPicker').classList.remove('show');updateLoraHead();
}
function updateLoraHead(){
  const active=[];document.querySelectorAll('.lora-slot .lpick').forEach(p=>{
    const v=p.dataset.val;if(v&&v!=='None')active.push(shortName(v));
  });
  $('loraVal').textContent=active.length?active.join(', '):'nenhuma';
}

/* ── Resolution cards with proportion icons ── */
function buildResCards(resolutions,def){
  const wrap=$('resBody');wrap.innerHTML='';
  selectedRes=def||resolutions[0]?.label||'';
  resolutions.forEach(r=>{
    const card=document.createElement('div');card.className='res-card'+(r.label===selectedRes?' sel':'');
    card.dataset.val=r.label;
    const icon=document.createElement('div');icon.className='res-icon';
    const maxDim=40;const ratio=r.w/r.h;let bw,bh;
    if(ratio>=1){bw=maxDim;bh=Math.round(maxDim/ratio);}else{bh=maxDim;bw=Math.round(maxDim*ratio);}
    const box=document.createElement('div');box.className='box';
    box.style.width=bw+'px';box.style.height=bh+'px';
    icon.appendChild(box);card.appendChild(icon);
    const info=document.createElement('div');
    const lab=document.createElement('div');lab.className='res-label';lab.textContent=r.label;
    const dim=document.createElement('div');dim.className='res-dim';dim.textContent=r.w+'×'+r.h;
    info.appendChild(lab);info.appendChild(dim);card.appendChild(info);
    card.onclick=()=>{
      wrap.querySelectorAll('.res-card').forEach(x=>x.classList.remove('sel'));
      card.classList.add('sel');selectedRes=r.label;$('resVal').textContent=r.label;$('resBar').classList.remove('open');
    };
    wrap.appendChild(card);
  });
  $('resVal').textContent=selectedRes;
}

/* ── Performance cards ── */
function buildPerfCards(perfs,def){
  const wrap=$('perfBody');wrap.innerHTML='';
  selectedPerf=def||perfs[0]||'SD3';
  perfs.forEach(p=>{
    const card=document.createElement('div');card.className='perf-card'+(p===selectedPerf?' sel':'');
    card.dataset.val=p;
    const nm=document.createElement('div');nm.className='perf-name';nm.textContent=p;card.appendChild(nm);
    card.onclick=()=>{
      wrap.querySelectorAll('.perf-card').forEach(x=>x.classList.remove('sel'));
      card.classList.add('sel');selectedPerf=p;$('perfVal').textContent=p;$('perfBar').classList.remove('open');
    };
    wrap.appendChild(card);
  });
  $('perfVal').textContent=selectedPerf;
}

/* ── Init ── */
async function init(){
  const o=await fetch('/cr/opcoes').then(r=>r.json());
  buildCkptCards(o.checkpoints,o.ckpt_thumbs||{},o.default.checkpoint);
  buildLoraSlots(o.loras,o.lora_thumbs||{});
  buildResCards(o.resolutions,o.default.resolution);
  buildPerfCards(o.performances,o.default.performance);
  (o.estilos||[]).forEach(e=>$('style').appendChild(opt(e,false)));
  await carregarPresets();
  // apply first negative preset by default
  const negs=PRESETS.negativos||[];
  if(negs.length){$('negative').value=negs[0].texto;$('presetNeg').value=negs[0].nome;}
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
  const out=[];document.querySelectorAll('.lora-slot').forEach(slot=>{
    const val=slot.querySelector('.lpick').dataset.val||'None';
    const w=parseFloat(slot.querySelector('input').value)||1;
    out.push({model:val,weight:w});
  });
  return out;
}

function showBtn(which){
  $('bGerar').style.display=which==='gerar'?'block':'none';
  $('bParar').style.display=which==='parar'?'block':'none';
  $('bRegen').style.display=which==='regen'?'block':'none';
}

async function gerar(){
  wasStopped=false;generating=true;showBtn('parar');setSt('enviando...');
  const styleSel=[...$('style').selectedOptions].map(o=>o.value);
  const payload={
    prompt:$('prompt').value,negative:$('negative').value,
    checkpoint:selectedCkpt,loras:coletarLoras(),
    style:styleSel.length?styleSel:null,
    resolution:selectedRes||null,performance:selectedPerf||null,
    image_number:parseInt($('image_number').value)||1,seed:parseInt($('seed').value)||-1
  };
  try{
    const r=await fetch('/cr/gerar',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)}).then(r=>r.json());
    if(!r.ok){setSt('erro: '+(r.erro||'?'));generating=false;showBtn('gerar');return;}
    setSt('gerando... veja na TV');watchNova();
  }catch(e){setSt('erro de conexao');generating=false;showBtn('gerar');}
}

async function parar(){
  setSt('parando...');
  try{await fetch('/cr/parar',{method:'POST'});}catch{}
  wasStopped=true;generating=false;showBtn('regen');setSt('parado');
}

function watchNova(){
  if(watching)return;watching=true;
  const iv=setInterval(async()=>{
    if(!generating&&!watching){clearInterval(iv);return;}
    try{const u=await fetch('/cr/ultima').then(r=>r.json());
      if(u.tem&&u.id!==lastId){lastId=u.id;setSt('imagem pronta na TV ✓');
        generating=false;watching=false;clearInterval(iv);showBtn('gerar');}
    }catch{}
  },1500);
  setTimeout(()=>{if(watching){generating=false;watching=false;clearInterval(iv);showBtn('gerar');}},600000);
}
init();
</script>
</body>
</html>"""


# ── PAGINA DA TV (PC) — imagem em tempo real + final ─────────────────────────────
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
#dot.gen{background:#f59e0b}
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
let lastFinalId=null,lastPreviewId=null;

async function tick(){
  try{
    const [uf,up]=await Promise.all([
      fetch('/cr/ultima',{cache:'no-store'}).then(r=>r.json()),
      fetch('/cr/preview',{cache:'no-store'}).then(r=>r.json())
    ]);
    // final image has priority
    if(uf.tem&&uf.id!==lastFinalId){
      lastFinalId=uf.id;lastPreviewId=null;
      const novo=new Image();
      novo.onload=()=>{img.src=novo.src;img.style.display='block';ph.style.display='none';};
      novo.src='/cr/imagem?t='+encodeURIComponent(uf.id);
      dot.className='live';setTimeout(()=>{dot.className='';},600);
    }
    // during generation show preview
    else if(up.tem&&up.id!==lastPreviewId){
      lastPreviewId=up.id;
      const novo=new Image();
      novo.onload=()=>{img.src=novo.src;img.style.display='block';ph.style.display='none';};
      novo.src='/cr/preview_img?t='+encodeURIComponent(up.id);
      dot.className='gen';
    }
  }catch(e){}
}
setInterval(tick,800);tick();
if('wakeLock'in navigator){const req=()=>navigator.wakeLock.request('screen').catch(()=>{});req();
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')req();});}
</script>
</body>
</html>"""
