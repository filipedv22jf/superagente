"""
scheduler.py — Scheduler de follow-ups multi-tenant.

Itera sobre todas as empresas ativas e processa
follow-ups e reengajamentos de cada uma separadamente.
"""

import asyncio
import logging
import httpx
from datetime import datetime, timezone, timedelta
from typing import Dict
from supabase import Client

from empresas_config import EmpresaConfig

try:
    from zoneinfo import ZoneInfo
    FUSO = ZoneInfo("America/Sao_Paulo")
except ImportError:
    try:
        import pytz
        FUSO = pytz.timezone("America/Sao_Paulo")
    except ImportError:
        FUSO = timezone(timedelta(hours=-3))

log = logging.getLogger(__name__)
INTERVALO = 900  # 15 minutos


def horario_permitido() -> bool:
    agora = datetime.now(FUSO)
    return 7 <= agora.hour < 21


def horas_desde(updated_at_str: str) -> float:
    try:
        if updated_at_str.endswith("Z"):
            updated_at_str = updated_at_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(updated_at_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return 0


# ============================================================================
# MENSAGENS DE FOLLOW-UP (genéricas — podem ser sobrescritas por empresa)
# ============================================================================

def msg_fu0(nome: str) -> str:
    n = nome if nome else "tudo bem"
    return (
        f"E aí, {n}! Já deu pra dar uma olhada na plataforma? 👀\n\n"
        "Se tiver qualquer dúvida pra começar, é só me avisar!"
    )

def msg_fu1(nome: str) -> str:
    n = nome if nome else "tudo bem"
    return f"E aí, {n}? 👋\nConseguiu explorar a plataforma? Ficou com alguma dúvida?"

def msg_fu2(nome: str) -> str:
    p = f"{nome}, " if nome else ""
    return (
        f"{p}tudo bem? 👋\n"
        "Você chegou a se cadastrar? Se ainda não testou, ainda dá tempo! É só me avisar."
    )

def msg_fu3(nome: str) -> str:
    p = f"{nome}, " if nome else ""
    return (
        f"{p}tudo bem? 🤙\n"
        "Se um dia quiser conhecer melhor, é só chamar. Boa sorte nos estudos! 💪"
    )

def msg_re1(nome: str) -> str:
    p = f"Oi {nome}! " if nome else "Oi! "
    return f"{p}Ficou alguma dúvida? Pode perguntar à vontade 😊"

def msg_re2(nome: str) -> str:
    p = f"{nome}, " if nome else ""
    return (
        f"{p}que tal testar a plataforma por 7 dias de graça? Sem precisar de cartão 😄\n"
        "Só me avisa que libero seu acesso!"
    )

def msg_re3(nome: str) -> str:
    p = f"{nome}, " if nome else ""
    return f"{p}tudo bem? 🤙\nSe um dia quiser conhecer, é só chamar. Boa sorte nos estudos!"


# ============================================================================
# ENVIO VIA Z-API
# ============================================================================

async def enviar_whatsapp(
    cfg: EmpresaConfig, telefone: str, texto: str,
    max_tentativas: int = 3
) -> bool:
    if not cfg.zapi_instance_id or not cfg.zapi_token:
        log.warning(f"[{cfg.empresa_id}] [SIMULADO] FU para {telefone}: {texto[:60]}")
        return True

    url = cfg.zapi_base_url + "/send-text"
    payload = {"phone": telefone, "message": texto}
    headers = {
        "Content-Type": "application/json",
        "client-token": cfg.zapi_client_token
    }

    for tentativa in range(1, max_tentativas + 1):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json=payload, headers=headers)
                r.raise_for_status()
                log.info(f"[{cfg.empresa_id}] ✅ FU enviado para {telefone} (t{tentativa})")
                return True
        except Exception as e:
            if tentativa < max_tentativas:
                await asyncio.sleep(tentativa)
            else:
                log.error(f"[{cfg.empresa_id}] ❌ FU falhou para {telefone}: {e}")
    return False


# ============================================================================
# HELPERS
# ============================================================================

async def _enviar_followup(
    supabase: Client, cfg: EmpresaConfig,
    conversa: dict, numero: int, mensagem: str
):
    telefone = conversa.get("telefone")
    nome = conversa.get("nome_aluno", "")
    log.info(f"[{cfg.empresa_id}] 📤 FU{numero} → {telefone}")

    enviado = await enviar_whatsapp(cfg, telefone, mensagem)

    if enviado:
        h = conversa.get("historico", []) or []
        h.append({
            "role": "assistant", "content": mensagem,
            "followup": numero,
            "enviado_em": datetime.now(timezone.utc).isoformat()
        })
        supabase.table("conversas").update({
            "contador_followups": numero,
            "status_conversa": "AGUARDAR_FOLLOW_UP",
            "historico": h[-20:],
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("telefone", telefone).eq("empresa_id", cfg.empresa_id).execute()
        log.info(f"[{cfg.empresa_id}] ✅ FU{numero} registrado para {telefone}")
    else:
        log.warning(f"[{cfg.empresa_id}] ⚠️ FU{numero} não registrado — será retentado")


async def _finalizar_inativo(
    supabase: Client, empresa_id: str, telefone: str
):
    agora = datetime.now(timezone.utc).isoformat()
    supabase.table("conversas").update({
        "status_conversa": "FINALIZADO_INATIVO", "updated_at": agora
    }).eq("telefone", telefone).eq("empresa_id", empresa_id).execute()
    try:
        supabase.table("leads").update({
            "status_conversa": "FINALIZADO_INATIVO", "updated_at": agora
        }).eq("telefone", telefone).eq("empresa_id", empresa_id).execute()
    except Exception:
        pass
    log.info(f"[{empresa_id}] 🏁 Inativo: {telefone}")


async def _enviar_reengajamento(
    supabase: Client, cfg: EmpresaConfig,
    conversa: dict, numero: int, mensagem: str
):
    telefone = conversa.get("telefone")
    log.info(f"[{cfg.empresa_id}] 📤 RE{numero} → {telefone}")

    enviado = await enviar_whatsapp(cfg, telefone, mensagem)

    if enviado:
        h = conversa.get("historico", []) or []
        h.append({
            "role": "assistant", "content": mensagem,
            "reengajamento": numero,
            "enviado_em": datetime.now(timezone.utc).isoformat()
        })
        supabase.table("conversas").update({
            "contador_reengajamento": numero,
            "historico": h[-20:],
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("telefone", telefone).eq("empresa_id", cfg.empresa_id).execute()


# ============================================================================
# PROCESSAMENTO POR EMPRESA
# ============================================================================

async def processar_followups_empresa(supabase: Client, cfg: EmpresaConfig):
    """Processa follow-ups de uma empresa específica."""
    try:
        r = (
            supabase.table("conversas")
            .select("*")
            .eq("empresa_id", cfg.empresa_id)
            .in_("status_conversa", ["AGUARDAR_FOLLOW_UP", "CADASTRO_ENVIADO", "FINALIZADO_ERRO"])
            .execute()
        )
        conversas = r.data or []
        log.info(f"[{cfg.empresa_id}] 📋 {len(conversas)} conversa(s) para follow-up")

        for c in conversas:
            if c.get("status_conversa") == "FINALIZADO_ERRO":
                log.warning(f"[{cfg.empresa_id}] ⚠️ FINALIZADO_ERRO: {c.get('telefone')}")
                continue

            telefone = c.get("telefone", "")
            nome = c.get("nome_aluno", "")
            updated_at = c.get("updated_at", "")
            fn = c.get("contador_followups", -1)
            if fn is None: fn = -1
            if not telefone or not updated_at: continue

            horas = horas_desde(updated_at)

            if fn == -1 and horas >= cfg.followup_0_horas:
                await _enviar_followup(supabase, cfg, c, 0, msg_fu0(nome))
            elif fn == 0 and horas >= cfg.followup_1_horas:
                await _enviar_followup(supabase, cfg, c, 1, msg_fu1(nome))
            elif fn == 1 and horas >= cfg.followup_2_horas:
                await _enviar_followup(supabase, cfg, c, 2, msg_fu2(nome))
            elif fn == 2 and horas >= cfg.followup_3_horas:
                await _enviar_followup(supabase, cfg, c, 3, msg_fu3(nome))
                await _finalizar_inativo(supabase, cfg.empresa_id, telefone)

    except Exception as e:
        log.error(f"[{cfg.empresa_id}] ❌ follow-ups: {e}")


async def processar_reengajamento_empresa(supabase: Client, cfg: EmpresaConfig):
    """Processa reengajamento de uma empresa específica."""
    try:
        r = (
            supabase.table("conversas")
            .select("*")
            .eq("empresa_id", cfg.empresa_id)
            .eq("status_conversa", "CONTINUAR")
            .execute()
        )
        conversas = r.data or []
        elegiveis = 0

        for c in conversas:
            telefone = c.get("telefone", "")
            nome = c.get("nome_aluno", "")
            updated_at = c.get("updated_at", "")
            re_n = c.get("contador_reengajamento", 0) or 0
            cnt_msgs = c.get("contador_mensagens", 0) or 0
            if not telefone or not updated_at: continue
            if cnt_msgs < 1: continue

            horas = horas_desde(updated_at)
            if horas < cfg.reengaj_1_horas: continue
            elegiveis += 1

            if re_n == 0 and horas >= cfg.reengaj_1_horas:
                await _enviar_reengajamento(supabase, cfg, c, 1, msg_re1(nome))
            elif re_n == 1 and horas >= cfg.reengaj_2_horas:
                await _enviar_reengajamento(supabase, cfg, c, 2, msg_re2(nome))
            elif re_n == 2 and horas >= cfg.reengaj_3_horas:
                await _enviar_reengajamento(supabase, cfg, c, 3, msg_re3(nome))
                await _finalizar_inativo(supabase, cfg.empresa_id, telefone)

        if elegiveis:
            log.info(f"[{cfg.empresa_id}] 📋 {elegiveis} elegível(eis) para reengajamento")

    except Exception as e:
        log.error(f"[{cfg.empresa_id}] ❌ reengajamento: {e}")


# ============================================================================
# LOOP PRINCIPAL
# ============================================================================

async def iniciar_scheduler(supabase: Client, empresas: Dict[str, EmpresaConfig]):
    """
    Loop principal do scheduler.
    Recebe referência ao dict de empresas (atualizado pelo main a cada hora).
    """
    log.info(f"🚀 Scheduler iniciado | Intervalo: {INTERVALO}s")

    while True:
        if not horario_permitido():
            log.info("🌙 Fora do horário (07:00-21:00). Follow-ups pausados.")
            await asyncio.sleep(INTERVALO)
            continue

        # Processa todas as empresas ativas com follow-up ativo
        tarefas_fu = [
            processar_followups_empresa(supabase, cfg)
            for cfg in empresas.values()
            if cfg.ativo and cfg.fluxo_followup
        ]
        tarefas_re = [
            processar_reengajamento_empresa(supabase, cfg)
            for cfg in empresas.values()
            if cfg.ativo and cfg.fluxo_followup
        ]

        if tarefas_fu or tarefas_re:
            await asyncio.gather(*tarefas_fu, *tarefas_re)

        await asyncio.sleep(INTERVALO)
