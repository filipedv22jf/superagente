"""
agente_core.py — Agente IA genérico multi-tenant.

Versão desacoplada do agente_mindmed.py original.
Recebe EmpresaConfig em vez de usar variáveis de ambiente globais.
Cada empresa tem seu próprio cliente OpenAI, prompt e configurações.
"""

import json
import time
import httpx
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional
from openai import OpenAI
from supabase import Client

from empresas_config import EmpresaConfig


# ============================================================================
# FERRAMENTAS — definição compartilhada (mesma para todas as empresas)
# ============================================================================

FERRAMENTAS = [
    {
        "type": "function",
        "function": {
            "name": "buscar_dados_aluno",
            "description": (
                "Busca os dados cadastrais de um lead/aluno no banco pelo telefone. "
                "Use SEMPRE antes de iniciar qualquer conversa para verificar "
                "se já está cadastrado e o que já foi coletado."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "telefone": {
                        "type": "string",
                        "description": "Telefone no formato E.164, ex: 5511999999999"
                    }
                },
                "required": ["telefone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "criar_ou_atualizar_lead",
            "description": (
                "Cria ou atualiza um lead no CRM. "
                "Chame sempre que coletar informações novas do aluno."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "telefone": {"type": "string"},
                    "nome": {"type": "string"},
                    "fase": {
                        "type": "string",
                        "enum": ["ciclo_basico", "ciclo_clinico", "internato", "formado", "residencia"]
                    },
                    "status_teste": {
                        "type": "string",
                        "enum": ["nao_iniciou", "testando", "testou_gostou", "testou_nao_gostou"]
                    },
                    "status_conversa": {
                        "type": "string",
                        "enum": [
                            "CONTINUAR", "CADASTRO_ENVIADO", "ACESSO_LIBERADO",
                            "AGUARDAR_FOLLOW_UP", "PASSAR_HUMANO", "FINALIZADO_SUCESSO",
                            "FINALIZADO_RECUSOU", "FINALIZADO_NAO_QUALIFICADO",
                            "FINALIZADO_INATIVO", "FINALIZADO_ERRO"
                        ]
                    }
                },
                "required": ["telefone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "registrar_acesso_trial",
            "description": (
                "Registra que o aluno se cadastrou e ativa o trial. "
                "Use APÓS o aluno confirmar cadastro. Máximo UMA vez por conversa."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "telefone": {"type": "string"},
                    "nome_aluno": {"type": "string"},
                    "fase": {"type": "string"},
                    "contexto": {"type": "string"}
                },
                "required": ["telefone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "notificar_time_comercial",
            "description": (
                "Envia notificação ao responsável via webhook e WhatsApp. "
                "Use para: cadastro confirmado, lead quer fechar, problema técnico, caso sensível."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "telefone": {"type": "string"},
                    "nome_aluno": {"type": "string"},
                    "fase": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": [
                            "ACESSO_LIBERADO", "CADASTRO_ENVIADO", "PASSAR_HUMANO",
                            "FINALIZADO_SUCESSO", "FINALIZADO_RECUSOU",
                            "FINALIZADO_NAO_QUALIFICADO", "FINALIZADO_INATIVO"
                        ]
                    },
                    "resumo_conversa": {"type": "string"},
                    "plano_interesse": {
                        "type": "string",
                        "enum": ["mensal", "anual", "bianual", "nao_informado"]
                    }
                },
                "required": ["telefone", "status"]
            }
        }
    }
]

# ============================================================================
# TAGS CRM
# ============================================================================

TAGS_STATUS = {
    "CONTINUAR":                  "🔵 EM_ATENDIMENTO",
    "CADASTRO_ENVIADO":           "🟡 CADASTRO_ENVIADO",
    "ACESSO_LIBERADO":            "🟢 TRIAL_ATIVO",
    "AGUARDAR_FOLLOW_UP":         "🔵 FOLLOW_UP",
    "PASSAR_HUMANO":              "🔴 FECHAR",
    "FINALIZADO_SUCESSO":         "✅ FECHOU",
    "FINALIZADO_RECUSOU":         "⚫ RECUSOU",
    "FINALIZADO_NAO_QUALIFICADO": "⚫ NAO_QUALIFICADO",
    "FINALIZADO_INATIVO":         "⚫ INATIVO",
    "FINALIZADO_ERRO":            "❌ ERRO",
}

STATUS_DISPARO_IMEDIATO = {"PASSAR_HUMANO", "ACESSO_LIBERADO", "CADASTRO_ENVIADO", "FINALIZADO_SUCESSO"}

MAX_TENTATIVAS_API = 3


# ============================================================================
# AGENTE GENÉRICO
# ============================================================================

class AgenteIA:
    """
    Agente IA genérico. Cada instância pertence a uma empresa.
    Recebe EmpresaConfig e Supabase client já configurados.
    """

    def __init__(self, config: EmpresaConfig, supabase: Client):
        self.config = config
        self.supabase = supabase
        self.empresa_id = config.empresa_id

        # Cliente OpenAI exclusivo desta empresa
        self.openai = OpenAI(api_key=config.openai_api_key)

        # Tabelas com prefixo de empresa_id para isolamento
        self._t_leads = "leads"
        self._t_conv = "conversas"

    # -------------------------------------------------------------------------
    # FERRAMENTAS — implementação
    # -------------------------------------------------------------------------

    def _buscar_dados_aluno(self, telefone: str) -> Dict:
        try:
            r = (
                self.supabase.table(self._t_leads)
                .select("*")
                .eq("telefone", telefone)
                .eq("empresa_id", self.empresa_id)
                .limit(1)
                .execute()
            )
            if r.data:
                return {"encontrado": True, "dados": r.data[0]}
            return {"encontrado": False, "dados": {}}
        except Exception as e:
            print(f"[{self.empresa_id}] ⚠️ buscar_dados_aluno: {e}")
            return {"encontrado": False, "dados": {}}

    def _criar_ou_atualizar_lead(self, telefone: str, **kwargs) -> Dict:
        try:
            dados = {
                "telefone": telefone,
                "empresa_id": self.empresa_id,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            for k, v in kwargs.items():
                if v is not None:
                    dados[k] = v

            r = (
                self.supabase.table(self._t_leads)
                .upsert(dados, on_conflict="telefone,empresa_id")
                .execute()
            )
            return {"sucesso": True, "dados": r.data}
        except Exception as e:
            print(f"[{self.empresa_id}] ❌ criar_ou_atualizar_lead: {e}")
            return {"sucesso": False, "erro": str(e)}

    def _registrar_acesso_trial(
        self, telefone: str, nome_aluno: str = None,
        fase: str = None, contexto: str = None
    ) -> Dict:
        try:
            self._criar_ou_atualizar_lead(
                telefone=telefone, nome=nome_aluno, fase=fase,
                status_teste="testando", status_conversa="ACESSO_LIBERADO"
            )
            self.supabase.table(self._t_conv).update({
                "contador_followups": -1,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("telefone", telefone).eq("empresa_id", self.empresa_id).execute()

            print(f"[{self.empresa_id}] ✅ Trial registrado | {telefone}")
            return {
                "sucesso": True,
                "mensagem": "Trial registrado. Chame notificar_time_comercial com ACESSO_LIBERADO."
            }
        except Exception as e:
            print(f"[{self.empresa_id}] ❌ registrar_acesso_trial: {e}")
            return {"sucesso": False, "erro": str(e)}

    def _notificar_time_comercial(
        self, telefone: str, status: str, nome_aluno: str = None,
        fase: str = None, resumo_conversa: str = None, plano_interesse: str = None
    ) -> Dict:
        tag = TAGS_STATUS.get(status, status)

        # Atualiza CRM
        try:
            self.supabase.table(self._t_conv).update({
                "tag_crm": tag,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("telefone", telefone).eq("empresa_id", self.empresa_id).execute()

            self.supabase.table(self._t_leads).update({
                "tag_crm": tag,
                "status_conversa": status,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("telefone", telefone).eq("empresa_id", self.empresa_id).execute()
        except Exception as e:
            print(f"[{self.empresa_id}] ⚠️ Erro ao salvar tag CRM: {e}")

        # Notifica responsável via WhatsApp
        eventos_criticos = ["PASSAR_HUMANO", "ACESSO_LIBERADO", "CADASTRO_ENVIADO"]
        if status in eventos_criticos:
            self._notificar_responsavel(
                telefone_lead=telefone, nome_lead=nome_aluno,
                status=status, fase=fase,
                plano_interesse=plano_interesse, resumo=resumo_conversa
            )

        # Webhook externo
        payload = {
            "evento": "notificacao_agente",
            "empresa_id": self.empresa_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "telefone": telefone, "nome_aluno": nome_aluno,
            "fase": fase, "status": status, "tag_crm": tag,
            "resumo_conversa": resumo_conversa, "plano_interesse": plano_interesse
        }

        if self.config.webhook_notificacao_url:
            try:
                with httpx.Client(timeout=10) as c:
                    r = c.post(self.config.webhook_notificacao_url, json=payload)
                r.raise_for_status()
            except Exception as e:
                print(f"[{self.empresa_id}] ⚠️ Webhook externo: {e}")

        return {"sucesso": True, "tag": tag}

    def _notificar_responsavel(
        self, telefone_lead: str, nome_lead: str, status: str,
        fase: str = None, plano_interesse: str = None, resumo: str = None
    ):
        """Envia WhatsApp para o responsável via Z-API."""
        if not getattr(self.config, 'notificacao_whatsapp', True):
            return
        if not self.config.responsavel_whatsapp:
            return
        if not self.config.zapi_instance_id or not self.config.zapi_token:
            return

        resumo = resumo or ""
        if status == "PASSAR_HUMANO":
            if resumo.startswith("🔧"):   titulo = "PROBLEMA TÉCNICO"
            elif resumo.startswith("📋"): titulo = "PROBLEMA DE CONTEÚDO"
            elif resumo.startswith("❓"): titulo = "DÚVIDA PENDENTE"
            elif resumo.startswith("🟢"): titulo = "LIBERAR ACESSO"
            else:                         titulo = "LEAD QUER FECHAR"
        elif status == "ACESSO_LIBERADO":
            titulo = "NOVO TRIAL — LIBERAR ACESSO AGORA"
        elif status == "CADASTRO_ENVIADO":
            titulo = "LEAD SE CADASTROU"
        elif status == "FINALIZADO_SUCESSO":
            titulo = "LEAD CONVERTIDO"
        else:
            titulo = "ATENÇÃO NECESSÁRIA"

        linhas = [
            f"*{titulo}*",
            f"Empresa: {self.config.nome}",
            f"Nome: {nome_lead or 'não informado'}",
            f"Telefone: +{telefone_lead}",
        ]
        if fase:           linhas.append(f"Fase: {fase}")
        if plano_interesse and plano_interesse != "nao_informado":
            linhas.append(f"Plano: {plano_interesse}")
        if resumo:         linhas.append(f"Contexto: {resumo}")

        try:
            url = self.config.zapi_base_url + "/send-text"
            with httpx.Client(timeout=15) as c:
                c.post(url, json={
                    "phone": self.config.responsavel_whatsapp,
                    "message": "\n".join(linhas)
                }, headers={
                    "Content-Type": "application/json",
                    "client-token": self.config.zapi_client_token
                })
        except Exception as e:
            print(f"[{self.empresa_id}] ⚠️ Notificar responsável: {e}")

    def _executar_ferramenta(self, nome: str, args: Dict) -> Dict:
        if nome == "buscar_dados_aluno":
            return self._buscar_dados_aluno(**args)
        if nome == "criar_ou_atualizar_lead":
            return self._criar_ou_atualizar_lead(**args)
        if nome == "registrar_acesso_trial":
            return self._registrar_acesso_trial(**args)
        if nome == "notificar_time_comercial":
            return self._notificar_time_comercial(**args)
        return {"erro": f"Ferramenta '{nome}' não encontrada"}

    # -------------------------------------------------------------------------
    # DISPARO PROGRAMÁTICO DE FERRAMENTAS CRÍTICAS
    # -------------------------------------------------------------------------

    def _disparar_criticos(
        self, telefone: str, status: str,
        dados_coletados: Dict, resumo: str, ja_chamadas: set
    ):
        nome = dados_coletados.get("nome")
        fase = dados_coletados.get("fase")

        if status == "ACESSO_LIBERADO":
            if "registrar_acesso_trial" not in ja_chamadas:
                self._registrar_acesso_trial(
                    telefone=telefone, nome_aluno=nome, fase=fase
                )
            if "notificar_time_comercial" not in ja_chamadas:
                self._notificar_time_comercial(
                    telefone=telefone, nome_aluno=nome, fase=fase,
                    status="ACESSO_LIBERADO",
                    resumo_conversa=resumo or "Aluno confirmou cadastro — liberar trial."
                )
        elif status in ("PASSAR_HUMANO", "CADASTRO_ENVIADO"):
            if "notificar_time_comercial" not in ja_chamadas:
                self._notificar_time_comercial(
                    telefone=telefone, nome_aluno=nome, fase=fase,
                    status=status,
                    resumo_conversa=resumo or f"Notificação automática — {status}"
                )
        elif status == "FINALIZADO_SUCESSO":
            if "notificar_time_comercial" not in ja_chamadas:
                self._notificar_time_comercial(
                    telefone=telefone, nome_aluno=nome, fase=fase,
                    status="FINALIZADO_SUCESSO",
                    resumo_conversa=resumo or "Lead convertido com sucesso."
                )

    # -------------------------------------------------------------------------
    # LOOP DO AGENTE
    # -------------------------------------------------------------------------

    def executar(
        self,
        telefone: str,
        historico: List[Dict],
        contador_mensagens: int = 0,
        tem_contexto_responsavel: bool = False
    ) -> Tuple[str, str, Dict]:

        if not self.config.prompt:
            return ("Problema técnico. Tente novamente.", "FINALIZADO_ERRO", {})

        prompt = self.config.prompt

        # ── BLOCOS DINÂMICOS — injetados conforme os toggles da empresa ──────

        # Fluxo comercial desligado → instrui o agente a não qualificar
        if not self.config.fluxo_comercial:
            prompt += (
                "\n\n⚙️ FLUXO COMERCIAL DESATIVADO: Não tente qualificar o contato "
                "nem coletar dados adicionais do lead. "
                "Apenas responda dúvidas e encaminhe problemas técnicos."
            )

        # Suporte desligado → escala imediatamente qualquer problema técnico
        if not self.config.fluxo_suporte:
            prompt += (
                "\n\n⚙️ SUPORTE TÉCNICO DESATIVADO: Se o contato relatar qualquer "
                "problema técnico, encaminhe imediatamente para a equipe sem tentar resolver. "
                "Não tente diagnosticar, não envie tutoriais."
            )

        # Caso sensível desligado → trata cancelamento/reembolso no fluxo normal
        if not self.config.fluxo_caso_sensivel:
            prompt += (
                "\n\n⚙️ CASO SENSÍVEL DESATIVADO: Cancelamento e reembolso não têm "
                "tratamento especial. Trate como qualquer outra dúvida e encaminhe "
                "para a equipe normalmente via PASSAR_HUMANO."
            )

        # Portão desligado → informa o agente que não há liberação manual
        if not self.config.portao_liberacao:
            prompt += (
                "\n\n⚙️ PORTÃO DE LIBERAÇÃO DESATIVADO: Todo contato novo é atendido "
                "imediatamente. Não há necessidade de aguardar aprovação manual."
            )

        # ─────────────────────────────────────────────────────────────────────

        if contador_mensagens >= 3:
            prompt += (
                f"\n\n⚠️ ATENÇÃO: Esta é a mensagem #{contador_mensagens + 1}. "
                "Se o lead não demonstrou interesse claro, encerre com elegância."
            )

        if tem_contexto_responsavel:
            prompt += (
                "\n\n⚠️ CONTEXTO: O histórico contém mensagens do responsável prefixadas. "
                "Leia para entender o que foi tratado e retome naturalmente."
            )

        prompt_final = (
            prompt
            + f"\n\nCONTEXTO DA SESSÃO: O telefone do lead é {telefone}. "
            "Use em TODAS as chamadas de ferramentas."
            "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            "\nFORMATO DE RETORNO — OBRIGATÓRIO"
            "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            "\nRetorne APENAS JSON puro. Nada antes. Nada depois."
            '\n{"resposta": "...", "status": "CONTINUAR", "resumo_notificacao": null, '
            '"dados_coletados": {"nome": null, "fase": null, "status_teste": null}}'
            "\n\nCAMPO status — valores aceitos:"
            "\nCONTINUAR | CADASTRO_ENVIADO | ACESSO_LIBERADO | AGUARDAR_FOLLOW_UP | "
            "PASSAR_HUMANO | FINALIZADO_SUCESSO | FINALIZADO_RECUSOU | "
            "FINALIZADO_NAO_QUALIFICADO | FINALIZADO_INATIVO"
            "\n\nREGRAS DE STATUS IMPORTANTES:"
            "\n- AGENDAMENTO CONFIRMADO = PASSAR_HUMANO obrigatório. Nunca FINALIZADO_SUCESSO direto."
            "\n- Use PASSAR_HUMANO quando o lead confirmar agendamento, querer fechar ou precisar de ação imediata."
            "\n- Use FINALIZADO_SUCESSO SOMENTE se o status anterior já foi PASSAR_HUMANO nesta conversa."
            "\n- Se tiver dúvida entre PASSAR_HUMANO e FINALIZADO_SUCESSO, use PASSAR_HUMANO."
            "\n\nCAMPO resumo_notificacao: preencha quando status for "
            "PASSAR_HUMANO, ACESSO_LIBERADO, CADASTRO_ENVIADO ou FINALIZADO_SUCESSO. "
            "Inclua nome do lead, interesse e contexto resumido. Null nos demais casos."
        )

        mensagens = [{"role": "system", "content": prompt_final}] + historico
        ferramentas_chamadas: set = set()

        for iteracao in range(5):
            print(f"[{self.empresa_id}] 🔄 Iteração #{iteracao + 1}")

            response = None
            for tentativa in range(1, MAX_TENTATIVAS_API + 1):
                try:
                    if tentativa > 1:
                        time.sleep(1)
                    response = self.openai.chat.completions.create(
                        model=self.config.modelo_ia,
                        messages=mensagens,
                        tools=FERRAMENTAS,
                        tool_choice="auto",
                        temperature=self.config.temperatura,
                        max_tokens=self.config.max_tokens,
                        response_format={"type": "json_object"},
                        timeout=30
                    )
                    break
                except Exception as e:
                    print(f"[{self.empresa_id}] ⚠️ API tentativa {tentativa}: {e}")
                    if tentativa == MAX_TENTATIVAS_API:
                        return ("Problema técnico momentâneo. Pode tentar de novo?", "CONTINUAR", {})

            choice = response.choices[0]
            msg = choice.message

            # Ferramentas
            if msg.tool_calls:
                mensagens.append(msg)
                for tc in msg.tool_calls:
                    nome_fn = tc.function.name
                    args = json.loads(tc.function.arguments)
                    if "telefone" in args and not args.get("telefone"):
                        args["telefone"] = telefone
                    # Garante empresa_id nos args se necessário
                    print(f"[{self.empresa_id}] 🔧 {nome_fn}({json.dumps(args)[:120]})")
                    resultado = self._executar_ferramenta(nome_fn, args)
                    ferramentas_chamadas.add(nome_fn)
                    print(f"[{self.empresa_id}] 📤 {json.dumps(resultado)[:120]}")
                    mensagens.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(resultado)
                    })
                continue

            # Resposta final
            if msg.content:
                conteudo = msg.content.strip()
                conteudo = conteudo.replace("\\n\\n", "\n\n").replace("\\n", "\n")

                try:
                    dados_json = json.loads(conteudo)
                    resposta = dados_json.get("resposta", "")
                    status = dados_json.get("status", "CONTINUAR")
                    dados_coletados = dados_json.get("dados_coletados", {})
                    resumo = dados_json.get("resumo_notificacao") or ""
                except json.JSONDecodeError:
                    print(f"[{self.empresa_id}] ⚠️ JSON inválido — tentando corrigir")
                    # Tenta extrair JSON de dentro do texto (às vezes o modelo envolve em markdown)
                    import re as _re
                    match = _re.search(r'\{.*\}', conteudo, _re.DOTALL)
                    if match:
                        try:
                            dados_json = json.loads(match.group())
                            resposta = dados_json.get("resposta", "")
                            status = dados_json.get("status", "CONTINUAR")
                            dados_coletados = dados_json.get("dados_coletados", {})
                            resumo = dados_json.get("resumo_notificacao") or ""
                            print(f"[{self.empresa_id}] ✅ JSON extraído do texto")
                        except Exception:
                            resposta = conteudo
                            status = "CONTINUAR"
                            dados_coletados = {}
                            resumo = ""
                    else:
                        # Última tentativa: pede ao modelo pra reformatar como JSON
                        try:
                            fix_messages = [
                                {"role": "system", "content": (
                                    "Você recebeu uma resposta em texto puro. "
                                    "Reformate-a OBRIGATORIAMENTE como JSON puro no formato:\n"
                                    '{"resposta": "<texto da resposta>", "status": "CONTINUAR", '
                                    '"resumo_notificacao": null, "dados_coletados": {"nome": null, "fase": null, "status_teste": null}}'
                                )},
                                {"role": "user", "content": f"Texto para reformatar:\n{conteudo}"}
                            ]
                            fix_r = self.openai.chat.completions.create(
                                model=self.config.modelo_ia,
                                messages=fix_messages,
                                response_format={"type": "json_object"},
                                max_tokens=500,
                                timeout=15
                            )
                            fix_content = fix_r.choices[0].message.content.strip()
                            dados_json = json.loads(fix_content)
                            resposta = dados_json.get("resposta") or conteudo or ""
                            status = dados_json.get("status", "CONTINUAR")
                            dados_coletados = dados_json.get("dados_coletados", {})
                            resumo = dados_json.get("resumo_notificacao") or ""
                            print(f"[{self.empresa_id}] ✅ JSON corrigido via retry")
                        except Exception:
                            # Se tudo falhar, usa o texto direto como resposta
                            resposta = conteudo if conteudo else "Hmm, pode repetir?"
                            status = "CONTINUAR"
                            dados_coletados = {}
                            resumo = ""
                            print(f"[{self.empresa_id}] ⚠️ Usando texto direto como fallback final")

                if isinstance(resposta, str):
                    resposta = resposta.replace("\\n\\n", "\n\n").replace("\\n", "\n")

                # Guard: se resposta ainda vazia após todo o processo, faz chamada simples
                if not resposta or not resposta.strip():
                    print(f"[{self.empresa_id}] ⚠️ Resposta vazia — chamada de recuperação")
                    try:
                        ultima_msg = next(
                            (m["content"] for m in reversed(historico) if m["role"] == "user"),
                            ""
                        )
                        rec = self.openai.chat.completions.create(
                            model=self.config.modelo_ia,
                            messages=[
                                {"role": "system", "content": (
                                    self.config.prompt +
                                    "\n\nResponda à mensagem do cliente de forma natural e direta. "
                                    "Retorne APENAS o texto da resposta, sem JSON."
                                )},
                                {"role": "user", "content": ultima_msg}
                            ],
                            max_tokens=300,
                            timeout=15
                        )
                        resposta = rec.choices[0].message.content.strip()
                        status = "CONTINUAR"
                        print(f"[{self.empresa_id}] ✅ Resposta recuperada | {len(resposta)} chars")
                    except Exception as e:
                        resposta = "pode repetir? não entendi direito"
                        print(f"[{self.empresa_id}] ❌ Recuperação falhou: {e}")

                status_validos = [
                    "CONTINUAR", "CADASTRO_ENVIADO", "ACESSO_LIBERADO",
                    "AGUARDAR_FOLLOW_UP", "PASSAR_HUMANO", "FINALIZADO_SUCESSO",
                    "FINALIZADO_RECUSOU", "FINALIZADO_NAO_QUALIFICADO",
                    "FINALIZADO_INATIVO", "FINALIZADO_ERRO"
                ]
                if status not in status_validos:
                    status = "CONTINUAR"

                if status in STATUS_DISPARO_IMEDIATO:
                    self._disparar_criticos(
                        telefone=telefone, status=status,
                        dados_coletados=dados_coletados,
                        resumo=resumo,
                        ja_chamadas=ferramentas_chamadas
                    )

                print(f"[{self.empresa_id}] ✅ Status: {status} | {len(resposta)} chars")
                return (resposta, status, dados_coletados)

        print(f"[{self.empresa_id}] ⚠️ Loop esgotado")
        return ("Hmm, tive um problema. Pode tentar de novo?", "CONTINUAR", {})


# ============================================================================
# GESTOR DE CONVERSAS — genérico por empresa
# ============================================================================

class GestorConversas:
    """
    Gestor de conversas por empresa.
    Substitui GestorConversasMindMed com suporte multi-tenant.
    """

    def __init__(self, config: EmpresaConfig, supabase: Client):
        self.config = config
        self.empresa_id = config.empresa_id
        self.supabase = supabase
        self.agente = AgenteIA(config=config, supabase=supabase)

    def processar_mensagem(self, telefone: str, mensagem: str) -> Dict:
        print(f"\n{'='*60}")
        print(f"[{self.empresa_id}] 📨 {telefone}: {mensagem[:80]}")

        try:
            estado = self._buscar_estado(telefone)

            status_finalizados = [
                "FINALIZADO_RECUSOU", "FINALIZADO_SUCESSO", "FINALIZADO_INATIVO",
                "FINALIZADO_NAO_QUALIFICADO", "FINALIZADO_ERRO", "AGUARDANDO_LIBERACAO"
            ]
            if estado.get("status_conversa") in status_finalizados:
                return {"resposta": "", "status": estado["status_conversa"], "deve_enviar": False}

            if estado.get("status_conversa") in ("PASSAR_HUMANO", "ACESSO_LIBERADO"):
                status_atual = estado["status_conversa"]
                historico = estado.get("historico", [])
                historico.append({"role": "user", "content": mensagem})
                self.supabase.table("conversas").update({
                    "historico": historico[-20:],
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }).eq("telefone", telefone).eq("empresa_id", self.empresa_id).execute()
                return {"resposta": "", "status": status_atual, "deve_enviar": False}

            historico = estado.get("historico", [])
            contador = estado.get("contador_mensagens", 0)
            historico.append({"role": "user", "content": mensagem})
            if len(historico) > 20:
                historico = historico[-20:]

            tem_ctx_resp = any(
                m.get("role") == "assistant" and m.get("content", "").startswith("[responsavel]:")
                for m in historico
            )

            resposta, status, dados_coletados = self.agente.executar(
                telefone=telefone,
                historico=historico,
                contador_mensagens=contador,
                tem_contexto_responsavel=tem_ctx_resp
            )

            historico.append({"role": "assistant", "content": resposta})
            contador += 1

            self._salvar_estado(
                telefone=telefone, historico=historico,
                status=status, contador=contador,
                dados_coletados=dados_coletados
            )

            return {
                "resposta": resposta, "status": status,
                "deve_enviar": bool(resposta),
                "dados_coletados": dados_coletados
            }

        except Exception as e:
            print(f"[{self.empresa_id}] ❌ Erro crítico: {e}")
            return {
                "resposta": "Ops, tive um problema técnico. Pode tentar novamente?",
                "status": "CONTINUAR", "deve_enviar": True, "dados_coletados": {}
            }

    def _buscar_estado(self, telefone: str) -> Dict:
        try:
            r = (
                self.supabase.table("conversas")
                .select("*")
                .eq("telefone", telefone)
                .eq("empresa_id", self.empresa_id)
                .limit(1)
                .execute()
            )
            if r.data:
                return r.data[0]
            return {
                "telefone": telefone, "empresa_id": self.empresa_id,
                "historico": [], "status_conversa": "CONTINUAR", "contador_mensagens": 0
            }
        except Exception as e:
            print(f"[{self.empresa_id}] ⚠️ _buscar_estado: {e}")
            return {
                "telefone": telefone, "empresa_id": self.empresa_id,
                "historico": [], "status_conversa": "CONTINUAR", "contador_mensagens": 0
            }

    def _salvar_estado(
        self, telefone: str, historico: list,
        status: str, contador: int, dados_coletados: Dict
    ):
        if len(historico) > 20:
            historico = historico[-20:]

        dados = {
            "telefone": telefone,
            "empresa_id": self.empresa_id,
            "historico": historico,
            "status_conversa": status,
            "contador_mensagens": contador,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        if dados_coletados.get("nome"):           dados["nome_aluno"] = dados_coletados["nome"]
        if dados_coletados.get("fase"):           dados["fase"] = dados_coletados["fase"]
        if dados_coletados.get("status_teste"):
            dados["status_teste"] = dados_coletados["status_teste"]

        self.supabase.table("conversas").upsert(
            dados, on_conflict="telefone,empresa_id"
        ).execute()
