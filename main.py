"""
main.py — Servidor FastAPI multi-tenant AgêntHub.

Cada empresa tem seu próprio webhook /webhook/zapi/{empresa_id}.
A configuração de cada empresa é carregada do Supabase na inicialização
e mantida em memória com refresh automático a cada hora.
"""

import os
import json
import httpx
import asyncio
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Set

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
from supabase import create_client, Client as SupabaseClient

from empresas_config import EmpresaConfig
from agente_core import GestorConversas
from scheduler import iniciar_scheduler

load_dotenv(override=True)

# ============================================================================
# CONFIGURAÇÃO BASE
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

app = FastAPI(title="AgênteHub API", version="1.0")

# Supabase compartilhado (credenciais da plataforma, não dos clientes)
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
_supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================================
# REGISTRO DE EMPRESAS EM MEMÓRIA
# ============================================================================
# empresa_id -> EmpresaConfig
_empresas: Dict[str, EmpresaConfig] = {}

# empresa_id -> GestorConversas
_gestores: Dict[str, GestorConversas] = {}

# empresa_id -> {telefone: asyncio.Lock}
_locks: Dict[str, Dict[str, asyncio.Lock]] = {}

# empresa_id -> {telefone: [msgs]}
_buffers: Dict[str, Dict] = {}
_buffer_lock = asyncio.Lock()

# Deduplicação local
_dedup_local: Dict[str, datetime] = {}
DEDUP_TTL = 30  # segundos

# ============================================================================
# WEBSOCKET — conexões ativas por empresa
# ============================================================================
# empresa_id -> set de WebSocket connections
_ws_connections: Dict[str, Set[WebSocket]] = {}


async def ws_broadcast(empresa_id: str, evento: dict):
    """Envia evento para todos os painéis conectados nessa empresa."""
    conns = _ws_connections.get(empresa_id, set())
    if not conns:
        return
    msg = json.dumps(evento)
    mortos = set()
    for ws in conns:
        try:
            await ws.send_text(msg)
        except Exception:
            mortos.add(ws)
    for ws in mortos:
        conns.discard(ws)


def _carregar_empresa(row: dict) -> EmpresaConfig:
    return EmpresaConfig.from_dict(row)


async def _carregar_todas_empresas():
    """Carrega todas as empresas ativas do Supabase."""
    global _empresas, _gestores
    try:
        r = _supabase.table("empresas").select("*").eq("ativo", True).execute()
        novas: Dict[str, EmpresaConfig] = {}
        for row in (r.data or []):
            try:
                cfg = _carregar_empresa(row)
                novas[cfg.empresa_id] = cfg
                if cfg.empresa_id not in _gestores:
                    _gestores[cfg.empresa_id] = GestorConversas(
                        config=cfg, supabase=_supabase
                    )
                else:
                    _gestores[cfg.empresa_id].config = cfg
                    _gestores[cfg.empresa_id].agente.config = cfg
                log.info(f"✅ Empresa carregada: {cfg.nome} ({cfg.empresa_id})")
            except Exception as e:
                log.error(f"❌ Erro ao carregar empresa: {e}")
        _empresas = novas
        log.info(f"📋 {len(_empresas)} empresa(s) ativa(s)")
    except Exception as e:
        log.error(f"❌ Erro ao carregar empresas: {e}")


async def _refresh_loop():
    """Recarrega configs a cada 60 minutos."""
    while True:
        await asyncio.sleep(3600)
        log.info("🔄 Refresh de configs de empresas...")
        await _carregar_todas_empresas()


# ============================================================================
# STARTUP / SHUTDOWN
# ============================================================================

@app.on_event("startup")
async def startup():
    await _carregar_todas_empresas()
    asyncio.create_task(_refresh_loop())
    asyncio.create_task(iniciar_scheduler(_supabase, _empresas))
    log.info("🚀 AgênteHub iniciado")


@app.on_event("shutdown")
async def shutdown():
    log.info("🛑 AgênteHub encerrado")


# ============================================================================
# HEALTH
# ============================================================================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "empresas_ativas": len(_empresas),
        "versao": "1.0"
    }


@app.get("/empresas")
async def listar_empresas():
    """Lista todas as empresas (ativas e inativas) para o painel."""
    try:
        r = _supabase.table("empresas").select(
            "empresa_id, nome, ativo, modelo_ia, responsavel_nome, responsavel_whatsapp"
        ).order("created_at").execute()
        return r.data or []
    except Exception as e:
        log.error(f"❌ listar_empresas: {e}")
        return [
            {
                "empresa_id": cfg.empresa_id,
                "nome": cfg.nome,
                "ativo": cfg.ativo,
                "modelo_ia": cfg.modelo_ia,
            }
            for cfg in _empresas.values()
        ]


# ============================================================================
# DEDUPLICAÇÃO
# ============================================================================

def _ja_processado(empresa_id: str, telefone: str, texto: str) -> bool:
    chave = hashlib.md5(f"{empresa_id}:{telefone}:{texto}".encode()).hexdigest()
    agora = datetime.now(timezone.utc)

    # Limpa expirados
    expirados = [k for k, t in _dedup_local.items()
                 if (agora - t).total_seconds() > DEDUP_TTL]
    for k in expirados:
        del _dedup_local[k]

    if chave in _dedup_local:
        return True

    _dedup_local[chave] = agora

    # Nível 2 — Supabase
    try:
        expira = agora + timedelta(seconds=DEDUP_TTL)
        _supabase.table("webhook_dedup").insert({
            "chave": chave,
            "empresa_id": empresa_id,
            "expira_em": expira.isoformat()
        }).execute()
        try:
            _supabase.table("webhook_dedup").delete().lt(
                "expira_em", agora.isoformat()
            ).execute()
        except Exception:
            pass
        return False
    except Exception as e:
        erro = str(e).lower()
        if "duplicate" in erro or "unique" in erro or "23505" in erro:
            return True
        return False


# ============================================================================
# UTILITÁRIOS
# ============================================================================

def _eh_grupo(body: dict) -> bool:
    if body.get("isGroupMsg"): return True
    for campo in ["phone", "chatId", "from"]:
        if "@g.us" in str(body.get(campo, "")): return True
    if body.get("groupId") or body.get("participantPhone"): return True
    return False


def _verificar_portao(empresa_id: str, telefone: str) -> Optional[str]:
    try:
        r = (_supabase.table("conversas")
             .select("status_conversa")
             .eq("telefone", telefone)
             .eq("empresa_id", empresa_id)
             .limit(1).execute())
        if r.data:
            return r.data[0].get("status_conversa")
        return None
    except Exception as e:
        log.error(f"[{empresa_id}] ❌ portão: {e}")
        return None


def _registrar_aguardando(empresa_id: str, telefone: str, mensagem: str):
    try:
        agora = datetime.now(timezone.utc).isoformat()
        r = (_supabase.table("conversas")
             .select("*")
             .eq("telefone", telefone)
             .eq("empresa_id", empresa_id)
             .limit(1).execute())

        if r.data:
            h = r.data[0].get("historico", []) or []
            h.append({"role": "user", "content": mensagem})
            _supabase.table("conversas").update({
                "historico": h[-20:], "updated_at": agora
            }).eq("telefone", telefone).eq("empresa_id", empresa_id).execute()
        else:
            _supabase.table("conversas").insert({
                "telefone": telefone, "empresa_id": empresa_id,
                "status_conversa": "AGUARDANDO_LIBERACAO",
                "historico": [{"role": "user", "content": mensagem}],
                "primeira_mensagem": mensagem[:500],
                "contador_mensagens": 0, "updated_at": agora
            }).execute()
            try:
                _supabase.table("leads").upsert({
                    "telefone": telefone, "empresa_id": empresa_id,
                    "status_conversa": "AGUARDANDO_LIBERACAO",
                    "tag_crm": "⏳ AGUARDANDO", "updated_at": agora
                }, on_conflict="telefone,empresa_id").execute()
            except Exception:
                pass

        log.info(f"[{empresa_id}] ⏳ {telefone} aguardando liberação")
    except Exception as e:
        log.error(f"[{empresa_id}] ❌ _registrar_aguardando: {e}")


# ============================================================================
# BUFFER DE MENSAGENS POR EMPRESA
# ============================================================================

def _get_lock(empresa_id: str, telefone: str) -> asyncio.Lock:
    if empresa_id not in _locks:
        _locks[empresa_id] = {}
    if telefone not in _locks[empresa_id]:
        _locks[empresa_id][telefone] = asyncio.Lock()
    return _locks[empresa_id][telefone]


async def _enfileirar(empresa_id: str, telefone: str, mensagem: str):
    async with _buffer_lock:
        if empresa_id not in _buffers:
            _buffers[empresa_id] = {}
        if telefone not in _buffers[empresa_id]:
            _buffers[empresa_id][telefone] = []
            asyncio.create_task(
                _processar_buffer(empresa_id, telefone)
            )
        _buffers[empresa_id][telefone].append(mensagem)


async def _processar_buffer(empresa_id: str, telefone: str):
    cfg = _empresas.get(empresa_id)
    janela = cfg.buffer_janela_segundos if cfg else 8.0
    await asyncio.sleep(janela)

    async with _buffer_lock:
        msgs = _buffers.get(empresa_id, {}).pop(telefone, [])

    if not msgs:
        return

    texto = "\n".join(msgs) if len(msgs) > 1 else msgs[0]
    if len(msgs) > 1:
        log.info(f"[{empresa_id}] 🔗 Buffer {telefone}: {len(msgs)} msgs fundidas")

    lock = _get_lock(empresa_id, telefone)
    async with lock:
        await _processar_e_responder(empresa_id, telefone, texto)


async def _processar_e_responder(empresa_id: str, telefone: str, mensagem: str):
    gestor = _gestores.get(empresa_id)
    if not gestor:
        log.error(f"[{empresa_id}] ❌ Gestor não encontrado")
        return
    try:
        resultado = await asyncio.to_thread(
            gestor.processar_mensagem, telefone=telefone, mensagem=mensagem
        )
        if resultado.get("deve_enviar") and resultado.get("resposta"):
            await _enviar_zapi(empresa_id, telefone, resultado["resposta"])

        # Notifica painel em tempo real via WebSocket
        await ws_broadcast(empresa_id, {
            "tipo": "mensagem_processada",
            "telefone": telefone,
            "status": resultado.get("status", "CONTINUAR"),
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        log.error(f"[{empresa_id}] ❌ processar_e_responder: {e}")


# ============================================================================
# ENVIO VIA Z-API
# ============================================================================

def _dividir_mensagem(texto: str) -> list:
    texto = texto.replace("\\n\\n", "\n\n").replace("\\n", "\n")
    if len(texto) <= 120:
        return [texto.strip()]
    partes = [p.strip() for p in texto.split("\n\n") if p.strip()]
    resultado = []
    for p in partes:
        if len(p) > 300:
            resultado.extend([s.strip() for s in p.split("\n") if s.strip()])
        else:
            resultado.append(p)
    return resultado or [texto.strip()]


async def _enviar_zapi(empresa_id: str, telefone: str, texto: str):
    cfg = _empresas.get(empresa_id)
    if not cfg or not cfg.zapi_instance_id or not cfg.zapi_token:
        log.warning(f"[{empresa_id}] Z-API não configurado — mensagem não enviada")
        log.info(f"[{empresa_id}] [SIMULADO] {telefone}: {texto[:80]}")
        return

    url = cfg.zapi_base_url + "/send-text"
    partes = _dividir_mensagem(texto)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for i, parte in enumerate(partes):
                palavras = len(parte.split())
                typing = min(max(round(palavras * 0.25), 2), 10)
                payload = {
                    "phone": telefone, "message": parte,
                    "delayMessage": 1, "delayTyping": typing
                }
                log.info(f"[{empresa_id}] ⌨️ {typing}s → parte {i+1}/{len(partes)}")
                r = await client.post(url, json=payload, headers={
                    "Content-Type": "application/json",
                    "client-token": cfg.zapi_client_token
                })
                r.raise_for_status()
                log.info(f"[{empresa_id}] ✅ Enviado para {telefone}")
    except Exception as e:
        log.error(f"[{empresa_id}] ❌ Z-API: {e}")


# ============================================================================
# WEBHOOK POR EMPRESA
# ============================================================================

@app.post("/webhook/zapi/{empresa_id}")
async def webhook_empresa(
    empresa_id: str, request: Request, background_tasks: BackgroundTasks
):
    cfg = _empresas.get(empresa_id)
    if not cfg:
        raise HTTPException(status_code=404, detail=f"Empresa '{empresa_id}' não encontrada")

    # Valida token
    client_token = request.headers.get("client-token", "")
    if cfg.zapi_client_token and client_token != cfg.zapi_client_token:
        raise HTTPException(status_code=403, detail="Token inválido")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body inválido")

    if _eh_grupo(body):
        return JSONResponse({"status": "ignored", "reason": "group"})

    telefone_raw = body.get("phone", "")
    if "@newsletter" in telefone_raw or "@broadcast" in telefone_raw:
        return JSONResponse({"status": "ignored", "reason": "newsletter"})

    if body.get("type", "") != "ReceivedCallback":
        return JSONResponse({"status": "ignored", "reason": "not_received"})

    if body.get("fromMe", False):
        return JSONResponse({"status": "ignored", "reason": "from_me"})

    telefone = (
        telefone_raw.split("@")[0].replace("+", "").replace(" ", "").strip()
    )

    texto = ""
    if body.get("text"):
        texto = body["text"].get("message", "").strip()
    elif body.get("caption"):
        texto = body.get("caption", "").strip()

    if not telefone or not texto:
        return JSONResponse({"status": "ignored", "reason": "no_text"})

    if _ja_processado(empresa_id, telefone, texto):
        return JSONResponse({"status": "ignored", "reason": "duplicate"})

    log.info(f"[{empresa_id}] 📨 {telefone}: {texto[:60]}")

    # Portão de liberação
    if cfg.portao_liberacao:
        status_atual = _verificar_portao(empresa_id, telefone)
        status_bloqueados = [
            "AGUARDANDO_LIBERACAO", "PASSAR_HUMANO",
            "FINALIZADO_SUCESSO", "FINALIZADO_RECUSOU",
            "FINALIZADO_NAO_QUALIFICADO", "FINALIZADO_INATIVO", "FINALIZADO_ERRO"
        ]
        if status_atual in status_bloqueados or status_atual is None:
            background_tasks.add_task(
                _registrar_aguardando, empresa_id, telefone, texto
            )
            return JSONResponse({"status": "waiting_approval"})

    background_tasks.add_task(_enfileirar, empresa_id, telefone, texto)
    return JSONResponse({"status": "received"})


# ============================================================================
# LIBERAR / RETOMAR / DELETAR
# ============================================================================

@app.post("/liberar/{empresa_id}/{telefone}")
async def liberar(empresa_id: str, telefone: str, background_tasks: BackgroundTasks):
    if empresa_id not in _empresas:
        raise HTTPException(status_code=404, detail="Empresa não encontrada")
    try:
        agora = datetime.now(timezone.utc).isoformat()

        r = _supabase.table("conversas").select("*").eq(
            "telefone", telefone
        ).eq("empresa_id", empresa_id).limit(1).execute()

        primeira_mensagem = None
        if r.data:
            h = r.data[0].get("historico", []) or []
            msgs_usuario = [
                m.get("content", "") for m in h
                if m.get("role") == "user" and m.get("content", "").strip()
            ]
            if msgs_usuario:
                primeira_mensagem = "\n".join(msgs_usuario)

        _supabase.table("conversas").update({
            "status_conversa": "CONTINUAR", "updated_at": agora
        }).eq("telefone", telefone).eq("empresa_id", empresa_id).execute()

        _supabase.table("leads").update({
            "status_conversa": "CONTINUAR",
            "tag_crm": "🔵 EM_ATENDIMENTO", "updated_at": agora
        }).eq("telefone", telefone).eq("empresa_id", empresa_id).execute()

        log.info(f"[{empresa_id}] ✅ Liberado: {telefone}")

        if primeira_mensagem:
            background_tasks.add_task(
                _processar_e_responder, empresa_id, telefone, primeira_mensagem
            )

        return JSONResponse({"sucesso": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/retomar/{empresa_id}/{telefone}")
async def retomar(empresa_id: str, telefone: str):
    if empresa_id not in _empresas:
        raise HTTPException(status_code=404, detail="Empresa não encontrada")
    try:
        agora = datetime.now(timezone.utc).isoformat()
        _supabase.table("conversas").update({
            "status_conversa": "CONTINUAR", "updated_at": agora
        }).eq("telefone", telefone).eq("empresa_id", empresa_id).execute()
        _supabase.table("leads").update({
            "status_conversa": "CONTINUAR", "updated_at": agora
        }).eq("telefone", telefone).eq("empresa_id", empresa_id).execute()
        log.info(f"[{empresa_id}] ✅ Retomado: {telefone}")
        return JSONResponse({"sucesso": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# GESTÃO DE EMPRESAS — API do painel
# ============================================================================

class EmpresaPayload(BaseModel):
    empresa_id: str
    nome: str
    zapi_instance_id: str = ""
    zapi_token: str = ""
    zapi_client_token: str = ""
    responsavel_whatsapp: str = ""
    responsavel_nome: str = "equipe"
    openai_api_key: str = ""
    modelo_ia: str = "gpt-4o-mini"
    temperatura: float = 0.5
    prompt: str = ""
    buffer_janela_segundos: float = 8.0
    fluxo_comercial: bool = True
    fluxo_suporte: bool = True
    fluxo_followup: bool = True
    portao_liberacao: bool = True
    ativo: bool = True


@app.post("/empresas")
async def criar_empresa(payload: EmpresaPayload):
    """Cria uma nova empresa e registra no Supabase."""
    try:
        dados = payload.dict()
        dados["updated_at"] = datetime.now(timezone.utc).isoformat()
        _supabase.table("empresas").upsert(
            dados, on_conflict="empresa_id"
        ).execute()
        # Recarrega em memória
        await _carregar_todas_empresas()
        log.info(f"✅ Empresa criada: {payload.empresa_id}")
        return JSONResponse({"sucesso": True, "empresa_id": payload.empresa_id})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/empresas/{empresa_id}")
async def atualizar_empresa(empresa_id: str, payload: EmpresaPayload):
    try:
        dados = payload.dict()
        dados["updated_at"] = datetime.now(timezone.utc).isoformat()
        _supabase.table("empresas").update(dados).eq(
            "empresa_id", empresa_id
        ).execute()
        await _carregar_todas_empresas()
        return JSONResponse({"sucesso": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/empresas/{empresa_id}")
async def buscar_empresa(empresa_id: str):
    """Busca dados completos de uma empresa para pré-preenchimento do wizard."""
    try:
        r = _supabase.table("empresas").select("*").eq(
            "empresa_id", empresa_id
        ).limit(1).execute()
        if r.data:
            return JSONResponse(r.data[0])
        raise HTTPException(status_code=404, detail="Empresa não encontrada")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ativar/{empresa_id}")
async def ativar_empresa(empresa_id: str):
    """Reativa uma empresa desativada."""
    try:
        _supabase.table("empresas").update({
            "ativo": True,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("empresa_id", empresa_id).execute()
        await _carregar_todas_empresas()
        return JSONResponse({"sucesso": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/empresas/{empresa_id}")
async def deletar_empresa(empresa_id: str, permanent: bool = False):
    """Desativa ou exclui permanentemente uma empresa."""
    try:
        if permanent:
            _supabase.table("empresas").delete().eq("empresa_id", empresa_id).execute()
            log.info(f"🗑️ Excluída permanentemente: {empresa_id}")
        else:
            _supabase.table("empresas").update({
                "ativo": False,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("empresa_id", empresa_id).execute()
            log.info(f"⏸️ Desativada: {empresa_id}")
        _empresas.pop(empresa_id, None)
        _gestores.pop(empresa_id, None)
        return JSONResponse({"sucesso": True})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# TESTE MANUAL
# ============================================================================

class MensagemTeste(BaseModel):
    empresa_id: str
    telefone: str
    mensagem: str


@app.post("/teste/mensagem")
async def testar_mensagem(body: MensagemTeste):
    gestor = _gestores.get(body.empresa_id)
    if not gestor:
        raise HTTPException(status_code=404, detail="Empresa não encontrada")
    resultado = await asyncio.to_thread(
        gestor.processar_mensagem,
        telefone=body.telefone, mensagem=body.mensagem
    )
    return JSONResponse(resultado)


# ============================================================================
# STATS — dados reais do Supabase para o node graph
# ============================================================================

@app.get("/stats/{empresa_id}")
async def stats_empresa(empresa_id: str):
    """Retorna estatísticas reais da empresa para o node graph."""
    if empresa_id not in _empresas:
        raise HTTPException(status_code=404, detail="Empresa não encontrada")
    try:
        # Busca todas as conversas da empresa
        r = (
            _supabase.table("conversas")
            .select("status_conversa, contador_mensagens, updated_at")
            .eq("empresa_id", empresa_id)
            .execute()
        )
        conversas = r.data or []

        from datetime import datetime, timezone, timedelta
        agora = datetime.now(timezone.utc)
        limite_24h = agora - timedelta(hours=24)

        total = len(conversas)
        ativos_24h = 0
        em_trial = 0
        follow_up = 0
        pra_fechar = 0
        mensagens_total = 0

        for c in conversas:
            status = c.get("status_conversa", "")
            cnt = c.get("contador_mensagens", 0) or 0
            mensagens_total += cnt

            upd = c.get("updated_at", "")
            try:
                if upd.endswith("Z"):
                    upd = upd[:-1] + "+00:00"
                dt = datetime.fromisoformat(upd)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= limite_24h:
                    ativos_24h += 1
            except Exception:
                pass

            if status in ("ACESSO_LIBERADO", "CADASTRO_ENVIADO"):
                em_trial += 1
            elif status == "AGUARDAR_FOLLOW_UP":
                follow_up += 1
            elif status == "PASSAR_HUMANO":
                pra_fechar += 1

        return JSONResponse({
            "empresa_id": empresa_id,
            "total_leads": total,
            "ativos_24h": ativos_24h,
            "em_trial": em_trial,
            "follow_up": follow_up,
            "pra_fechar": pra_fechar,
            "mensagens_total": mensagens_total,
            "timestamp": agora.isoformat()
        })
    except Exception as e:
        log.error(f"[{empresa_id}] ❌ stats: {e}")
        return JSONResponse({
            "empresa_id": empresa_id,
            "total_leads": 0, "ativos_24h": 0,
            "em_trial": 0, "follow_up": 0,
            "pra_fechar": 0, "mensagens_total": 0,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })


# ============================================================================
# PAINEL
# ============================================================================

@app.websocket("/ws/{empresa_id}")
async def websocket_empresa(websocket: WebSocket, empresa_id: str):
    """WebSocket para o painel receber eventos em tempo real."""
    await websocket.accept()
    if empresa_id not in _ws_connections:
        _ws_connections[empresa_id] = set()
    _ws_connections[empresa_id].add(websocket)
    log.info(f"[{empresa_id}] 🔌 Painel conectado via WebSocket")
    try:
        while True:
            # Mantém a conexão viva aguardando ping do cliente
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        _ws_connections.get(empresa_id, set()).discard(websocket)
        log.info(f"[{empresa_id}] 🔌 Painel desconectado")
    except Exception:
        _ws_connections.get(empresa_id, set()).discard(websocket)


@app.get("/painel")
async def painel():
    return FileResponse("painel.html")


# ============================================================================
# PONTO DE ENTRADA
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
