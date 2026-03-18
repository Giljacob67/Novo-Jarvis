# Jarvis Pessoal

Assistente pessoal de produtividade via Telegram, construído com Python e FastAPI.

Integra com Telegram, OpenAI, Google Calendar, Google Tasks e Gmail.

## Arquitetura

- **Backend**: Python 3.12 + FastAPI + SQLAlchemy + SQLite
- **IA**: OpenAI Responses API com function calling
- **Bot**: Telegram Bot API via webhooks
- **Memória**: SQLite local (tabelas: users, conversations, messages, memory_items, action_logs, google_credentials)
- **Google**: OAuth 2.0 + Google Calendar API + Google Tasks API + Gmail API
- **Deploy**: Replit (workflow único, porta 8000)

## Endpoints

| Método | Caminho | Descrição |
|--------|---------|-----------|
| GET | `/health` | Health check — retorna `{"status": "ok"}` |
| GET | `/me/day` | Resumo do dia (dados reais se Google conectado, mock se não) |
| POST | `/webhooks/telegram` | Recebe updates do webhook do Telegram |
| GET | `/auth/google/start` | Inicia fluxo OAuth Google (redireciona para Google) |
| GET | `/auth/google/callback` | Callback OAuth Google (troca code por tokens) |
| GET | `/auth/google/status` | Status da conexão Google (inclui gmail_enabled, calendar_enabled, tasks_enabled) |
| POST | `/auth/google/disconnect` | Revoga token no Google e remove tokens locais |

## Comandos do Bot Telegram

| Comando | Descrição |
|---------|-----------|
| `/start` | Mensagem de boas-vindas e lista de comandos |
| `/help` | Lista de comandos disponíveis |
| `/myday` | Resumo do dia (agenda, tarefas, e-mails prioritários) |
| `/headlines [tema]` | Manchetes automáticas por tema (`tecnologia`, `ia`, `brasil`, `mundo`) |
| `/remember <texto>` | Salva uma anotação/lembrete |
| `/memories` | Lista anotações recentes |
| `/connectgoogle` | Envia link para conectar conta Google |
| `/google` | Mostra status da conexão Google (Calendar, Tasks, Gmail, Drive) |
| `/tasks` | Lista tarefas pendentes do Google Tasks |
| `/newtask <titulo>` | Cria nova tarefa no Google Tasks |
| `/newevent <titulo> \| <início> \| <fim>` | Cria evento no Google Calendar |
| `/inbox` | Lista e-mails recentes da inbox |
| `/emailsearch <consulta>` | Busca e-mails com sintaxe Gmail |
| `/thread <thread_id>` | Mostra thread de e-mail completa |
| `/drafts` | Lista rascunhos de e-mail |
| `/draftemail <para> \| <assunto> \| <corpo>` | Cria rascunho de e-mail |
| `/replydraft <message_id> \| <corpo>` | Cria rascunho de resposta a um e-mail |
| `/senddraft <draft_id>` | Envia rascunho de e-mail |
| `/inboxsummary` | Resumo da inbox (e-mails não lidos importantes) |
| `/drive` | Lista arquivos recentes do Google Drive |
| `/drivesearch <consulta>` | Busca arquivos por nome no Google Drive |
| `/drivefile <file_id>` | Mostra detalhes de um arquivo do Drive por ID |
| `/drivesummary <file_id> \| <foco opcional>` | Gera resumo de um arquivo do Drive |
| Foto/print + legenda | O Jarvis extrai texto da imagem para interpretar pedidos (ex.: agendar prazo) |
| `/voiceon` | Ativa respostas por áudio |
| `/voiceoff` | Desativa respostas por áudio |
| `/voicestatus` | Status das respostas por áudio |
| `/transcribe` | Informação sobre transcrição (basta enviar áudio) |
| Nota de voz / Áudio | Transcreve e processa como texto |
| Texto livre | Conversa com o assistente via OpenAI |

## 1. Configurar Google OAuth

### Criar credenciais no Google Cloud Console

1. Acesse [Google Cloud Console](https://console.cloud.google.com/)
2. Crie ou selecione um projeto
3. Ative as APIs: **Google Calendar API**, **Google Tasks API**, **Gmail API** e **Google Drive API**
4. Vá em **APIs & Services > Credentials**
5. Clique em **Create Credentials > OAuth 2.0 Client ID**
6. Tipo: **Web application**
7. Em **Authorized redirect URIs**, adicione a URL de callback

### ⚠️ IMPORTANTE: Redirect URI

A redirect URI cadastrada no Google Cloud **DEVE corresponder EXATAMENTE** ao valor de `GOOGLE_REDIRECT_URI` configurado no Replit:

- **Protocolo**: `https://` (não `http://`)
- **Domínio**: exatamente igual (ex: `jarvis-pessoal.replit.app`)
- **Path**: `/auth/google/callback` (sem trailing slash!)
- **Exemplo**: `https://jarvis-pessoal.replit.app/auth/google/callback`

Se houver qualquer diferença (mesmo um `/` no final), o Google retornará erro `redirect_uri_mismatch`.

### Escopos utilizados

- `https://www.googleapis.com/auth/calendar.events` — Ler e criar eventos
- `https://www.googleapis.com/auth/tasks` — Ler e criar tarefas
- `https://www.googleapis.com/auth/gmail.readonly` — Ler e-mails e metadados
- `https://www.googleapis.com/auth/gmail.compose` — Criar rascunhos e enviar e-mails
- `https://www.googleapis.com/auth/drive.metadata.readonly` — Listar e buscar arquivos no Drive (metadados)

### ⚠️ Sobre escopos do Gmail

Os escopos `gmail.readonly` e `gmail.compose` são classificados como "sensíveis" pelo Google. Isso significa que:

1. Para uso pessoal/teste, o app pode funcionar sem verificação (com aviso "app não verificado")
2. Para distribuição pública, o app precisará passar pela verificação do Google
3. O usuário verá uma tela de aviso do Google ao autorizar — basta clicar em "Avançado" > "Acessar"

### Reconsentimento (reconectar com novos escopos)

Se a conta Google já estava conectada antes da Fase 4 (só com Calendar/Tasks), será necessário reconectar para adicionar os escopos de Gmail:

1. No Telegram, envie `/google` para ver o status atual
2. Se Gmail aparecer como ❌, envie `/connectgoogle`
3. Autorize os novos escopos na tela do Google
4. Após redirecionamento, o Gmail estará habilitado

O sistema preserva `access_type=offline` e `include_granted_scopes=true`, então os escopos anteriores são mantidos.

## 2. Como rodar no Workspace (desenvolvimento)

### Pré-requisitos

O projeto roda no Replit. As dependências Python são instaladas automaticamente.

### Nota importante sobre o Replit

No Replit, o servidor **deve escutar em `0.0.0.0`** (não em `localhost` ou `127.0.0.1`), e **apenas uma porta externa é exposta**. Este projeto usa a porta `8000`. O workflow está configurado para:

```
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Secrets no Replit

Cadastre os seguintes Secrets no painel do Replit (Tools > Secrets):

| Secret | Obrigatório? | Descrição |
|--------|:---:|-----------|
| `TELEGRAM_BOT_TOKEN` | Sim | Token do bot do Telegram (obtido via @BotFather) |
| `TELEGRAM_WEBHOOK_SECRET` | Sim | String secreta para validar webhooks do Telegram |
| `TELEGRAM_ALLOWED_USER_ID` | Recomendado | Seu user ID do Telegram (string) para filtrar mensagens |
| `OPENAI_API_KEY` | Sim | Chave da API OpenAI |
| `OPENAI_MODEL` | Opcional | Modelo a usar (padrão: `gpt-5-mini`) |
| `APP_BASE_URL` | Sim | URL pública do app (ex: `https://jarvis-pessoal.replit.app`). Em Railway, use o domínio público atual do serviço (sem `/api` e sem `/` no final). |
| `GOOGLE_CLIENT_ID` | Sim | Client ID do Google OAuth |
| `GOOGLE_CLIENT_SECRET` | Sim | Client Secret do Google OAuth |
| `GOOGLE_REDIRECT_URI` | Sim | URI de callback (ex: `https://jarvis-pessoal.replit.app/auth/google/callback`) |
| `GOOGLE_OAUTH_SCOPES` | Opcional | Escopos Calendar/Tasks (padrão: `calendar.events tasks`) |
| `GOOGLE_GMAIL_ENABLED` | Opcional | `true` (padrão) ou `false` para desabilitar Gmail |
| `GOOGLE_GMAIL_SCOPES` | Opcional | Escopos Gmail (padrão: `gmail.readonly gmail.compose`) |
| `GOOGLE_DRIVE_ENABLED` | Opcional | `true` (padrão) ou `false` para desabilitar integração com Drive |
| `GOOGLE_DRIVE_SCOPES` | Opcional | Escopo Drive (padrão: `drive.metadata.readonly`) |
| `GMAIL_INBOX_QUERY_DEFAULT` | Opcional | Query padrão para /inbox (padrão: `in:inbox newer_than:7d`) |
| `GMAIL_MAX_LIST_RESULTS` | Opcional | Máximo de e-mails listados (padrão: `10`) |
| `OPENAI_TRANSCRIBE_MODEL` | Opcional | Modelo de transcrição (padrão: `gpt-4o-mini-transcribe`) |
| `OPENAI_TTS_MODEL` | Opcional | Modelo de síntese de voz (padrão: `gpt-4o-mini-tts`) |
| `AUDIO_API_KEY` | Opcional* | Chave para transcrição/TTS. Se vazio, usa aliases de OpenAI (`OPENAI_API_KEY`, `OPENAIAPIKEY`, etc.) |
| `AUDIO_BASE_URL` | Opcional | Base URL do provedor de áudio compatível com OpenAI (padrão: `https://api.openai.com/v1`) |
| `VOICE_RESPONSES_ENABLED` | Opcional | `false` (padrão) — ativa respostas por áudio globalmente |
| `VOICE_RESPONSE_VOICE` | Opcional | Voz para TTS (padrão: `alloy`) |
| `MAX_AUDIO_FILE_MB` | Opcional | Limite de tamanho de áudio em MB (padrão: `19`, máx: `20`) |
| `TEMP_AUDIO_DIR` | Opcional | Diretório para arquivos temporários de áudio (padrão: `/tmp/jarvis_audio`) |
| `GOOGLE_ENCRYPTION_KEY` | Reservado | Para criptografia futura de tokens |
| `APP_ENV` | Opcional | `development` (padrão) ou `production` |
| `TIMEZONE` | Opcional | Fuso horário, padrão `America/Sao_Paulo` |
| `JARVIS_DATABASE_URL` | Opcional | Padrão `sqlite:///./jarvis.db` |

### Nota sobre o workspace

O Replit pode ter workflows adicionais pré-configurados (ex: api-server, mockup-sandbox) que são gerenciados pela plataforma. Eles não fazem parte do Jarvis Pessoal e podem ser ignorados. O único workflow relevante é **"Jarvis Pessoal"**.

### Rodando

Basta iniciar o workflow "Jarvis Pessoal" no Replit. O servidor estará disponível em `https://<seu-repl>.replit.dev`.

### Testes

```bash
pytest tests/ -v
```

### Configurar Webhook do Telegram

**Opção 1 — Script utilitário:**

```bash
APP_BASE_URL=https://seu-repl.replit.app python scripts/set_telegram_webhook.py
```

**Opção 2 — curl direto:**

```bash
curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://SEU-REPL.replit.app/webhooks/telegram",
    "secret_token": "SEU_WEBHOOK_SECRET"
  }'
```

**Verificar webhook atual:**

```bash
python scripts/get_telegram_webhook_info.py
```

### Conectar conta Google

1. Configure os Secrets `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` e `GOOGLE_REDIRECT_URI`
2. No Telegram, envie `/connectgoogle` ao bot
3. Clique no link recebido
4. Autorize as permissões no Google (Calendar, Tasks, Gmail e Drive)
5. Após redirecionamento, o Telegram confirmará a conexão
6. Envie `/google` para verificar o status (Calendar ✅, Tasks ✅, Gmail ✅, Drive ✅)
7. Envie `/myday` para ver dados reais da agenda e e-mails

### Testando comandos Google

1. `/connectgoogle` — recebe link para conectar Google
2. `/google` — mostra "conectada" e status de cada serviço
3. `/tasks` — lista tarefas pendentes do Google Tasks
4. `/newtask Comprar leite` — cria tarefa
5. `/newevent Reunião | 2026-03-16 09:00 | 2026-03-16 10:00` — cria evento
6. `/myday` — resumo com dados reais

### Testando comandos Gmail

1. `/inbox` — lista últimos e-mails da inbox
2. `/emailsearch from:joao@email.com` — busca e-mails por remetente
3. `/emailsearch is:unread subject:relatório` — busca por assunto
4. `/emailsearch newer_than:3d` — e-mails dos últimos 3 dias
5. `/thread <thread_id>` — ver thread completa (ID obtido via /inbox)
6. `/draftemail joao@email.com | Reunião amanhã | Olá João, podemos marcar?` — criar rascunho
7. `/replydraft <message_id> | Obrigado, confirmo presença!` — criar rascunho de resposta
8. `/drafts` — listar rascunhos existentes
9. `/senddraft <draft_id>` — enviar rascunho
10. `/inboxsummary` — resumo rápido da inbox

### Testando comandos Drive

1. `/drive` — lista arquivos recentes do Drive
2. `/drivesearch contrato` — busca arquivos por nome
3. `/drivefile <file_id>` — mostra metadados e link do arquivo
4. `/drivesummary <file_id> | decisões e prazos` — gera resumo focado

### Voz no Telegram

O Jarvis suporta mensagens de voz e arquivos de áudio no Telegram. O fluxo é:

1. Você envia uma **nota de voz** (gravada diretamente no Telegram) ou um **arquivo de áudio** (anexo)
2. O Jarvis baixa o arquivo, transcreve com a OpenAI Audio API e processa como texto normal
3. A transcrição entra no mesmo fluxo do assistente (memória, Calendar, Tasks, Gmail, tools)
4. Você recebe a resposta em texto, mostrando a transcrição original
5. Se `VOICE_RESPONSES_ENABLED=true` e você ativou `/voiceon`, a resposta também vem em áudio

**Diferença entre voice e audio no Telegram:**
- **Voice** (nota de voz): gravada pelo botão de microfone do Telegram, formato OGG/Opus
- **Audio** (arquivo de áudio): arquivo MP3/M4A/WAV enviado como anexo

**Ativar/desativar respostas por áudio:**
1. O administrador define `VOICE_RESPONSES_ENABLED=true` no ambiente (controle global)
2. Cada usuário ativa com `/voiceon` e desativa com `/voiceoff`
3. Ambas as condições precisam estar ativas para receber áudio

**Envio de áudio de resposta (fallback):**
O Jarvis tenta enviar a resposta como voice note (`sendVoice`). Se o formato não for compatível (Telegram exige OGG/Opus), faz fallback para `sendAudio`.

**Limite de tamanho:** 19 MB por padrão (margem de segurança para o limite de 20 MB do `getFile` do Telegram). Configurável via `MAX_AUDIO_FILE_MB`.

**Metadados de confiança:** Quando a resposta da OpenAI inclui logprobs ou sinais de confiança na transcrição, eles são persistidos no campo `transcription_raw_json` da tabela `voice_message_logs`.

### ⚠️ Nota sobre queries com datas na Gmail API

A Gmail API interpreta queries com datas literais (ex: `after:2026/03/15`) no fuso **PST (Pacific Standard Time)**, não no fuso local do usuário. Para buscas por data exata em código, o sistema usa timestamps Unix em segundos (via helper `date_to_gmail_after_query`) para evitar ambiguidades.

Para queries de usuário via `/emailsearch`, use formatos relativos como `newer_than:3d` quando possível, ou esteja ciente da diferença de fuso em datas absolutas.

## 3. Como publicar (deploy always-on)

### Preparação

1. Certifique-se de que todos os Secrets obrigatórios estão cadastrados
2. Configure `APP_ENV=production` nos Secrets
3. Configure `APP_BASE_URL` com a URL pública do deploy (ex: `https://jarvis-pessoal.replit.app`)
4. **Railway:** confirme que `APP_BASE_URL` e o domínio público do serviço são o mesmo host. Se divergir, o webhook do Telegram pode ficar em 404.
4. Se quiser usar PostgreSQL em produção, configure `JARVIS_DATABASE_URL` com a connection string

### Publicando

Use o botão "Deploy" no Replit e selecione "Autoscale" ou "Reserved VM". O comando de execução é:

```
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Pós-deploy

1. Configure o webhook do Telegram apontando para a URL de produção
2. Verifique o endpoint `/health` para confirmar que está funcionando
3. Conecte a conta Google via `/connectgoogle` no Telegram

### Smoke test Railway (recomendado)

Após cada deploy no Railway, rode:

```bash
APP_BASE_URL="https://novo-jarvis-production.up.railway.app" \
TELEGRAM_BOT_TOKEN="..." \
TELEGRAM_WEBHOOK_SECRET="..." \
TELEGRAM_ALLOWED_USER_ID="7995994992" \
python scripts/railway_smoke_test.py
```

Se quiser corrigir automaticamente webhook divergente:

```bash
python scripts/railway_smoke_test.py --fix-webhook
```

## Roadmap

### Fase 2 ✅
- Bot Telegram funcional com comandos e texto livre
- Integração real com OpenAI (Responses API + function calling)
- Memória local em SQLite (anotações, conversas, histórico)
- Idempotência por update_id
- Segurança: webhook secret + filtro por user_id

### Fase 3 ✅
- Google OAuth 2.0 (fluxo completo com refresh token)
- Google Calendar: listar e criar eventos reais
- Google Tasks: listar e criar tarefas reais
- Fallback para dados mockados quando Google não conectado
- Novos comandos Telegram: /connectgoogle, /google, /tasks, /newtask, /newevent
- Novas tools OpenAI: list_tasks, create_task, list_upcoming_events, create_event

### Fase 4 ✅
- Gmail API: ler inbox, buscar e-mails, ver threads, criar rascunhos, enviar
- Reconsentimento OAuth para adicionar escopos Gmail
- Status detalhado: gmail_enabled, calendar_enabled, tasks_enabled
- 8 novos comandos Telegram: /inbox, /emailsearch, /thread, /drafts, /draftemail, /replydraft, /senddraft, /inboxsummary
- 8 novas tools OpenAI para Gmail
- /replydraft com headers MIME completos (In-Reply-To, References, threadId)
- Envio via texto livre cria rascunho (segurança); /senddraft para enviar
- /myday inclui e-mails prioritários quando Gmail conectado

### Fase 5 ✅
- Voz no Telegram: transcrição real + resposta por áudio opcional
- Recebe notas de voz (OGG/Opus) e arquivos de áudio (MP3/M4A/WAV)
- Transcrição via OpenAI Audio API (modelo configurável: gpt-4o-mini-transcribe)
- Transcrição cai no mesmo fluxo de inteligência (memória, tools, Gmail, Calendar, Tasks)
- Respostas por áudio opcionais via TTS (modelo configurável: gpt-4o-mini-tts)
- Fallback sendVoice → sendAudio para compatibilidade
- Tabela VoiceMessageLog com metadados completos e transcription_raw_json
- Comandos /voiceon, /voiceoff, /voicestatus para preferência por usuário
- Limite de 19 MB (margem para getFile do Telegram)
- Limpeza automática de arquivos temporários

### Fase futura
- Voz em tempo real via Realtime API (usará client secrets efêmeros — nunca a chave principal no navegador)
- Edição/exclusão de eventos e tarefas (com aprovação explícita)
- Push notifications do Gmail
- Anexos de e-mail
- Criptografia de tokens Google
