import json
import logging
from typing import Any

from app.config import settings
from app.prompts import SYSTEM_PROMPT, format_memories_context
from app.models.action_log import ActionLog

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "type": "function",
        "name": "get_my_day",
        "description": "Retorna a agenda, tarefas e e-mails prioritários do dia do usuário",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "save_memory",
        "description": "Salva uma anotação ou lembrete para o usuário",
        "parameters": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "O conteúdo da anotação"},
                "category": {
                    "type": "string",
                    "description": "Categoria da memória (ex: general, task, reminder)",
                    "default": "general",
                },
            },
            "required": ["note"],
        },
    },
    {
        "type": "function",
        "name": "list_memories",
        "description": "Lista as memórias/anotações recentes do usuário",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de memórias a retornar",
                    "default": 5,
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "list_tasks",
        "description": "Lista tarefas pendentes do Google Tasks",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de tarefas a retornar",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "create_task",
        "description": "Cria uma nova tarefa no Google Tasks",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Título da tarefa"},
                "notes": {"type": "string", "description": "Notas/descrição da tarefa"},
                "due": {"type": "string", "description": "Data de vencimento (YYYY-MM-DD)"},
            },
            "required": ["title"],
        },
    },
    {
        "type": "function",
        "name": "update_task",
        "description": "Atualiza uma tarefa existente no Google Tasks",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "ID da tarefa a ser atualizada"},
                "title": {"type": "string", "description": "Novo título da tarefa"},
                "notes": {"type": "string", "description": "Novas notas da tarefa"},
                "due": {"type": "string", "description": "Nova data de vencimento (YYYY-MM-DD)"},
            },
            "required": ["task_id"],
        },
    },
    {
        "type": "function",
        "name": "delete_task",
        "description": "Exclui uma tarefa do Google Tasks",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "ID da tarefa a ser excluída"},
            },
            "required": ["task_id"],
        },
    },
    {
        "type": "function",
        "name": "list_upcoming_events",
        "description": "Lista próximos eventos do Google Calendar",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Número de dias à frente para buscar eventos",
                    "default": 7,
                },
                "limit": {
                    "type": "integer",
                    "description": "Número máximo de eventos",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "create_event",
        "description": "Cria um evento no Google Calendar",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Título do evento"},
                "start_time": {"type": "string", "description": "Início (YYYY-MM-DD HH:MM)"},
                "end_time": {"type": "string", "description": "Fim (YYYY-MM-DD HH:MM)"},
                "timezone": {"type": "string", "description": "Timezone (padrão: America/Sao_Paulo)"},
                "description": {"type": "string", "description": "Descrição do evento"},
                "location": {"type": "string", "description": "Local do evento"},
            },
            "required": ["title", "start_time", "end_time"],
        },
    },
    {
        "type": "function",
        "name": "update_event",
        "description": "Atualiza um evento existente no Google Calendar",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID do evento a ser atualizado"},
                "title": {"type": "string", "description": "Novo título do evento"},
                "start_time": {"type": "string", "description": "Novo início (YYYY-MM-DD HH:MM)"},
                "end_time": {"type": "string", "description": "Novo fim (YYYY-MM-DD HH:MM)"},
                "description": {"type": "string", "description": "Nova descrição do evento"},
                "location": {"type": "string", "description": "Novo local do evento"},
            },
            "required": ["event_id"],
        },
    },
    {
        "type": "function",
        "name": "delete_event",
        "description": "Exclui um evento do Google Calendar",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID do evento a ser excluído"},
            },
            "required": ["event_id"],
        },
    },
    {
        "type": "function",
        "name": "get_google_connection_status",
        "description": "Verifica se a conta Google do usuário está conectada",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_gmail_connection_status",
        "description": "Verifica se o Gmail do usuário está conectado e com os escopos corretos",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_inbox_summary",
        "description": "Retorna um resumo dos e-mails não lidos mais importantes da inbox",
        "parameters": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Número máximo de e-mails no resumo",
                    "default": 5,
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "search_emails",
        "description": "Busca e-mails usando sintaxe de busca do Gmail (ex: from:user@example.com is:unread)",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Query de busca no formato Gmail"},
                "max_results": {
                    "type": "integer",
                    "description": "Número máximo de resultados",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "get_email_thread",
        "description": "Retorna todas as mensagens de uma thread de e-mail",
        "parameters": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string", "description": "ID da thread do Gmail"},
            },
            "required": ["thread_id"],
        },
    },
    {
        "type": "function",
        "name": "create_email_draft",
        "description": "Cria um rascunho de e-mail novo",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Endereço do destinatário"},
                "subject": {"type": "string", "description": "Assunto do e-mail"},
                "body": {"type": "string", "description": "Corpo do e-mail em texto simples"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "type": "function",
        "name": "create_reply_draft",
        "description": "Cria um rascunho de resposta a um e-mail existente, com headers MIME corretos (In-Reply-To, References)",
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "ID da mensagem original no Gmail"},
                "body": {"type": "string", "description": "Corpo da resposta em texto simples"},
            },
            "required": ["message_id", "body"],
        },
    },
    {
        "type": "function",
        "name": "list_email_drafts",
        "description": "Lista os rascunhos de e-mail existentes",
        "parameters": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Número máximo de rascunhos",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "send_email_draft",
        "description": "Envia um e-mail. Por segurança, quando chamado via texto livre, NÃO envia diretamente — cria um rascunho e retorna instrução para usar /senddraft. Pode receber draft_id existente ou dados de composição (to, subject, body) para criar rascunho automaticamente.",
        "parameters": {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string", "description": "ID de um rascunho existente a enviar"},
                "to": {"type": "string", "description": "Endereço do destinatário (para criar rascunho)"},
                "subject": {"type": "string", "description": "Assunto (para criar rascunho)"},
                "body": {"type": "string", "description": "Corpo do e-mail (para criar rascunho)"},
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "browser_start_session",
        "description": "Inicia uma sessão de browser para automação supervisionada. O domínio deve estar na lista de permissões.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL inicial a abrir"},
            },
            "required": ["url"],
        },
    },
    {
        "type": "function",
        "name": "browser_open_url",
        "description": "Navega para uma URL dentro de uma sessão de browser ativa.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "ID da sessão de browser"},
                "url": {"type": "string", "description": "URL a navegar"},
            },
            "required": ["session_id", "url"],
        },
    },
    {
        "type": "function",
        "name": "browser_capture_screenshot",
        "description": "Captura uma screenshot da página atual e salva como artefato.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "ID da sessão de browser"},
            },
            "required": ["session_id"],
        },
    },
    {
        "type": "function",
        "name": "browser_extract_text",
        "description": "Extrai o texto visível da página atual do browser.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "ID da sessão de browser"},
            },
            "required": ["session_id"],
        },
    },
    {
        "type": "function",
        "name": "browser_click",
        "description": "Clica em um elemento da página usando seletor CSS. Ações sensíveis (botões de pagamento, exclusão etc.) requerem aprovação.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "ID da sessão de browser"},
                "selector": {"type": "string", "description": "Seletor CSS do elemento"},
            },
            "required": ["session_id", "selector"],
        },
    },
    {
        "type": "function",
        "name": "browser_fill",
        "description": "Preenche um campo de formulário com um valor.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "ID da sessão de browser"},
                "selector": {"type": "string", "description": "Seletor CSS do campo"},
                "value": {"type": "string", "description": "Valor a preencher"},
            },
            "required": ["session_id", "selector", "value"],
        },
    },
    {
        "type": "function",
        "name": "browser_select_option",
        "description": "Seleciona uma opção em um elemento <select>.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "ID da sessão de browser"},
                "selector": {"type": "string", "description": "Seletor CSS do <select>"},
                "value": {"type": "string", "description": "Valor da opção a selecionar"},
            },
            "required": ["session_id", "selector", "value"],
        },
    },
    {
        "type": "function",
        "name": "browser_wait_for_selector",
        "description": "Aguarda um elemento aparecer na página.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "ID da sessão de browser"},
                "selector": {"type": "string", "description": "Seletor CSS a aguardar"},
                "timeout_ms": {"type": "integer", "description": "Timeout em ms (opcional)"},
            },
            "required": ["session_id", "selector"],
        },
    },
    {
        "type": "function",
        "name": "browser_download_file",
        "description": "Faz download de um arquivo clicando em um elemento da página. O arquivo é salvo e registrado como artefato.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "ID da sessão de browser"},
                "trigger_selector": {"type": "string", "description": "Seletor CSS do botão/link de download"},
            },
            "required": ["session_id", "trigger_selector"],
        },
    },
    {
        "type": "function",
        "name": "browser_get_page_summary",
        "description": "Retorna URL, título e texto resumido da página atual do browser.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "ID da sessão de browser"},
            },
            "required": ["session_id"],
        },
    },
    {
        "type": "function",
        "name": "browser_list_sessions",
        "description": "Lista as sessões de browser do usuário (ativas e recentes).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "browser_close_session",
        "description": "Encerra uma sessão de browser ativa.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "ID da sessão a encerrar"},
            },
            "required": ["session_id"],
        },
    },
    {
        "type": "function",
        "name": "get_pending_approvals",
        "description": "Lista aprovações pendentes do usuário",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "create_approval",
        "description": "Cria uma aprovação pendente para uma ação sensível (envio de e-mail, criação de evento, follow-up). A ação NÃO é executada — fica aguardando aprovação do usuário via /approve.",
        "parameters": {
            "type": "object",
            "properties": {
                "action_type": {
                    "type": "string",
                    "description": "Tipo da ação: send_email_draft, create_followup_task, create_calendar_event_from_ai, send_proactive_followup_message",
                },
                "title": {"type": "string", "description": "Título curto da aprovação"},
                "summary": {"type": "string", "description": "Resumo descritivo da ação"},
                "payload": {"type": "object", "description": "Dados necessários para executar a ação"},
            },
            "required": ["action_type", "title", "summary"],
        },
    },
    {
        "type": "function",
        "name": "run_workflow",
        "description": "Executa um workflow/playbook. Disponíveis: lead_followup (params: empresa|email|contexto), meeting_prep, inbox_triage",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nome do workflow"},
                "params": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Parâmetros do workflow",
                },
            },
            "required": ["name"],
        },
    },
    {
        "type": "function",
        "name": "get_morning_briefing",
        "description": "Gera o briefing matinal com agenda, tarefas, e-mails e sugestões do dia",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_evening_review",
        "description": "Gera o fechamento do dia com resumo do que foi feito, pendências e proposta para amanhã",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_proactive_suggestions",
        "description": "Retorna sugestões proativas: eventos próximos, tarefas vencendo, follow-ups e rascunhos pendentes",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "browser_get_elements",
        "description": "Lista elementos interativos (botões, links, campos) da página atual do browser.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "ID da sessão de browser"},
            },
            "required": ["session_id"],
        },
    },
]

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {"type": "object", "properties": {}, "required": []}),
        },
    }
    for tool in TOOLS
]

SENSITIVE_KEYWORDS = [
    "apagar", "deletar", "excluir",
    "cancelar evento", "editar agenda", "modificar evento", "remover",
    "delete_event", "delete_task", "update_event", "update_task",
]


def _runtime_capabilities_context() -> str:
    browser_enabled = settings.browser_automation_enabled
    allowed_raw = settings.browser_allowed_domains.strip()
    allowed_domains = [d.strip() for d in allowed_raw.split(",") if d.strip()]
    browser_effective = browser_enabled and bool(allowed_domains)

    if allowed_domains:
        domains_text = ", ".join(allowed_domains)
    else:
        domains_text = "nenhum"

    return (
        "Capacidades de runtime (não invente):\n"
        f"- browser_automation_enabled: {str(browser_enabled).lower()}\n"
        f"- browser_access_effective: {str(browser_effective).lower()}\n"
        f"- browser_allowed_domains: {domains_text}\n\n"
        "Regra para perguntas sobre internet:\n"
        "- Se browser_access_effective=true: responda que há acesso supervisionado à web em domínios permitidos e ofereça /webresearch <url>.\n"
        "- Se browser_access_effective=false: responda que a navegação web não está ativa no momento e explique de forma breve o motivo (browser desativado ou sem domínios permitidos).\n"
        "- Nunca diga que tem acesso irrestrito à internet."
    )


class OpenAIService:
    def __init__(self) -> None:
        self._client: Any = None
        self._client_provider: str = ""

    def _effective_provider(self) -> str:
        provider = (settings.llm_provider or "openai").strip().lower()
        return provider if provider in {"openai", "openrouter"} else "openai"

    def _effective_model(self) -> str:
        if self._effective_provider() == "openrouter" and settings.openrouter_model.strip():
            return settings.openrouter_model.strip()
        return settings.openai_model

    def _get_client(self) -> Any:
        provider = self._effective_provider()
        if provider == "openrouter":
            api_key = settings.openrouter_api_key or settings.openai_api_key
        else:
            api_key = settings.openai_api_key

        if not api_key:
            return None

        if self._client is None or self._client_provider != provider:
            from openai import OpenAI
            if provider == "openrouter":
                headers: dict[str, str] = {
                    "X-Title": "Jarvis Pessoal",
                }
                if settings.effective_base_url:
                    headers["HTTP-Referer"] = settings.effective_base_url
                self._client = OpenAI(
                    api_key=api_key,
                    base_url=settings.openrouter_base_url,
                    default_headers=headers,
                )
            else:
                self._client = OpenAI(api_key=api_key)
            self._client_provider = provider
        return self._client

    async def generate_reply(
        self,
        user_id: str,
        user_text: str,
        recent_messages: list[dict[str, str]],
        memories: list,
        tool_executor: Any = None,
        db: Any = None,
    ) -> str:
        client = self._get_client()
        if client is None:
            return (
                "A integração de LLM ainda não foi configurada. "
                "Peça ao administrador para definir OPENAI_API_KEY ou OPENROUTER_API_KEY. "
                "Enquanto isso, você pode usar /myday, /remember e /memories."
            )

        system_content = SYSTEM_PROMPT
        mem_ctx = format_memories_context(memories)
        if mem_ctx:
            system_content += f"\n\n{mem_ctx}"
        system_content += f"\n\n{_runtime_capabilities_context()}"

        input_messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
        ]
        input_messages.extend(recent_messages)
        input_messages.append({"role": "user", "content": user_text})

        if self._effective_provider() == "openrouter":
            return await self._generate_reply_openrouter(
                client=client,
                user_id=user_id,
                user_text=user_text,
                input_messages=input_messages,
                tool_executor=tool_executor,
                db=db,
            )

        max_rounds = settings.openai_max_tool_rounds
        for round_num in range(max_rounds + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": self._effective_model(),
                    "input": input_messages,
                }
                if round_num < max_rounds:
                    kwargs["tools"] = TOOLS
                response = client.responses.create(**kwargs)
            except Exception as e:
                logger.exception("OpenAI API error: %s", e)
                return "Desculpe, ocorreu um erro ao processar sua mensagem. Tente novamente em instantes."

            has_tool_calls = False
            final_text = ""

            for item in response.output:
                if item.type == "function_call" and tool_executor and round_num < max_rounds:
                    has_tool_calls = True
                    tool_name = item.name
                    try:
                        tool_args = json.loads(item.arguments) if item.arguments else {}
                    except json.JSONDecodeError:
                        tool_args = {}

                    if _is_sensitive_action(tool_name, tool_args, user_text):
                        tool_result = _log_sensitive_action(db, tool_name, tool_args, user_id)
                    else:
                        tool_result = await tool_executor(tool_name, tool_args, db, user_id)

                    input_messages.append(item)
                    input_messages.append({
                        "type": "function_call_output",
                        "call_id": item.call_id,
                        "output": json.dumps(tool_result, ensure_ascii=False),
                    })

                elif item.type == "message":
                    for content_part in item.content:
                        if content_part.type == "output_text":
                            final_text += content_part.text

            if not has_tool_calls or round_num >= max_rounds:
                if final_text:
                    return final_text
                break

        return final_text or "Desculpe, não consegui gerar uma resposta. Tente novamente."

    async def _generate_reply_openrouter(
        self,
        client: Any,
        user_id: str,
        user_text: str,
        input_messages: list[dict[str, Any]],
        tool_executor: Any = None,
        db: Any = None,
    ) -> str:
        messages: list[dict[str, Any]] = [
            {"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in input_messages
        ]

        max_rounds = settings.openai_max_tool_rounds
        for round_num in range(max_rounds + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": self._effective_model(),
                    "messages": messages,
                }
                if round_num < max_rounds:
                    kwargs["tools"] = CHAT_TOOLS
                response = client.chat.completions.create(**kwargs)
            except Exception as e:
                logger.exception("OpenRouter/OpenAI-compatible API error: %s", e)
                return "Desculpe, ocorreu um erro ao processar sua mensagem. Tente novamente em instantes."

            choice = response.choices[0].message if response.choices else None
            if not choice:
                return "Desculpe, não consegui gerar uma resposta. Tente novamente."

            tool_calls = choice.tool_calls or []
            assistant_text = choice.content or ""
            if isinstance(assistant_text, list):
                assistant_text = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in assistant_text
                )

            if tool_calls and tool_executor and round_num < max_rounds:
                messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_text or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments or "{}",
                                },
                            }
                            for tc in tool_calls
                        ],
                    }
                )

                for tc in tool_calls:
                    tool_name = tc.function.name
                    try:
                        tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        tool_args = {}

                    if _is_sensitive_action(tool_name, tool_args, user_text):
                        tool_result = _log_sensitive_action(db, tool_name, tool_args, user_id)
                    else:
                        tool_result = await tool_executor(tool_name, tool_args, db, user_id)

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(tool_result, ensure_ascii=False),
                        }
                    )
                continue

            if assistant_text:
                return assistant_text
            return "Desculpe, não consegui gerar uma resposta. Tente novamente."

        return "Desculpe, não consegui gerar uma resposta. Tente novamente."


def _is_sensitive_action(tool_name: str, tool_args: dict, user_text: str) -> bool:
    text_lower = user_text.lower()
    return any(kw in text_lower for kw in SENSITIVE_KEYWORDS)


def _log_sensitive_action(db: Any, tool_name: str, tool_args: dict, user_id: str) -> dict:
    if db:
        log_entry = ActionLog(
            event_type=f"sensitive_action_blocked:{tool_name}",
            status="blocked",
            details_json=json.dumps(
                {"tool": tool_name, "args": tool_args, "user_id": user_id},
                ensure_ascii=False,
            ),
        )
        db.add(log_entry)
        db.commit()
    return {
        "status": "blocked",
        "message": "Esta ação será habilitada numa fase futura com aprovação explícita.",
    }
