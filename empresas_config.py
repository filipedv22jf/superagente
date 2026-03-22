"""
EmpresaConfig — modelo de configuração por empresa.

Cada empresa tem suas próprias credenciais, prompt, modelo e
configurações de follow-up. O sistema carrega essa config do
Supabase ao iniciar e a passa para o agente e o scheduler.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EmpresaConfig:
    # Identificação
    empresa_id: str          # slug único: "mindmed", "clinica_x"
    nome: str                # nome legível: "MindMed"
    ativo: bool = True

    # WhatsApp / Z-API
    zapi_instance_id: str = ""
    zapi_token: str = ""
    zapi_client_token: str = ""

    # Responsável (quem recebe notificações críticas)
    responsavel_whatsapp: str = ""
    responsavel_nome: str = "equipe"

    # IA
    openai_api_key: str = ""
    modelo_ia: str = "gpt-4o-mini"
    temperatura: float = 0.5
    max_tokens: int = 1024

    # Prompt (texto completo armazenado no banco)
    prompt: str = ""

    # Webhook externo (opcional)
    webhook_notificacao_url: str = ""

    # Buffer de mensagens rápidas (segundos)
    buffer_janela_segundos: float = 8.0

    # Follow-up timings (horas)
    followup_0_horas: float = 1.0
    followup_1_horas: int = 24
    followup_2_horas: int = 48
    followup_3_horas: int = 72
    reengaj_1_horas: int = 48
    reengaj_2_horas: int = 96
    reengaj_3_horas: int = 144

    # Fluxos ativos
    fluxo_comercial: bool = True
    fluxo_suporte: bool = True
    fluxo_followup: bool = True
    fluxo_caso_sensivel: bool = True
    portao_liberacao: bool = True

    @property
    def zapi_base_url(self) -> str:
        return (
            f"https://api.z-api.io/instances/{self.zapi_instance_id}"
            f"/token/{self.zapi_token}"
        )

    @classmethod
    def from_dict(cls, d: dict) -> "EmpresaConfig":
        """Constrói EmpresaConfig a partir de um dict do Supabase."""
        return cls(
            empresa_id=d["empresa_id"],
            nome=d["nome"],
            ativo=d.get("ativo", True),
            zapi_instance_id=d.get("zapi_instance_id", ""),
            zapi_token=d.get("zapi_token", ""),
            zapi_client_token=d.get("zapi_client_token", ""),
            responsavel_whatsapp=d.get("responsavel_whatsapp", ""),
            responsavel_nome=d.get("responsavel_nome", "equipe"),
            openai_api_key=d.get("openai_api_key", ""),
            modelo_ia=d.get("modelo_ia", "gpt-4o-mini"),
            temperatura=float(d.get("temperatura", 0.5)),
            max_tokens=int(d.get("max_tokens", 1024)),
            prompt=d.get("prompt", ""),
            webhook_notificacao_url=d.get("webhook_notificacao_url", ""),
            buffer_janela_segundos=float(d.get("buffer_janela_segundos", 8.0)),
            followup_0_horas=float(d.get("followup_0_horas", 1.0)),
            followup_1_horas=int(d.get("followup_1_horas", 24)),
            followup_2_horas=int(d.get("followup_2_horas", 48)),
            followup_3_horas=int(d.get("followup_3_horas", 72)),
            reengaj_1_horas=int(d.get("reengaj_1_horas", 48)),
            reengaj_2_horas=int(d.get("reengaj_2_horas", 96)),
            reengaj_3_horas=int(d.get("reengaj_3_horas", 144)),
            fluxo_comercial=d.get("fluxo_comercial", True),
            fluxo_suporte=d.get("fluxo_suporte", True),
            fluxo_followup=d.get("fluxo_followup", True),
            fluxo_caso_sensivel=d.get("fluxo_caso_sensivel", True),
            portao_liberacao=d.get("portao_liberacao", True),
        )

    def to_dict(self) -> dict:
        """Serializa para salvar no Supabase."""
        return {
            "empresa_id": self.empresa_id,
            "nome": self.nome,
            "ativo": self.ativo,
            "zapi_instance_id": self.zapi_instance_id,
            "zapi_token": self.zapi_token,
            "zapi_client_token": self.zapi_client_token,
            "responsavel_whatsapp": self.responsavel_whatsapp,
            "responsavel_nome": self.responsavel_nome,
            "openai_api_key": self.openai_api_key,
            "modelo_ia": self.modelo_ia,
            "temperatura": self.temperatura,
            "max_tokens": self.max_tokens,
            "prompt": self.prompt,
            "webhook_notificacao_url": self.webhook_notificacao_url,
            "buffer_janela_segundos": self.buffer_janela_segundos,
            "followup_0_horas": self.followup_0_horas,
            "followup_1_horas": self.followup_1_horas,
            "followup_2_horas": self.followup_2_horas,
            "followup_3_horas": self.followup_3_horas,
            "reengaj_1_horas": self.reengaj_1_horas,
            "reengaj_2_horas": self.reengaj_2_horas,
            "reengaj_3_horas": self.reengaj_3_horas,
            "fluxo_comercial": self.fluxo_comercial,
            "fluxo_suporte": self.fluxo_suporte,
            "fluxo_followup": self.fluxo_followup,
            "fluxo_caso_sensivel": self.fluxo_caso_sensivel,
            "portao_liberacao": self.portao_liberacao,
        }
