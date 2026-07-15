# Publicar o Painel KAT (IA nativa, sem senha para o usuário)

Este pacote roda o painel **e** a IA (Claude) num servidor. Quem visualiza **abre o link e usa** —
não digita senha. A chave da API fica **só no servidor** (nunca no navegador nem no repositório).

## Antes de tudo
1. **Gere uma chave NOVA** no console da Anthropic (console.anthropic.com → API Keys) e
   **revogue a que você compartilhou no chat** — ela está comprometida.
2. **Coloque um LIMITE DE GASTO** na chave (console → Limits/Usage). Essa é a proteção
   de custo mais importante.

## Como o acesso funciona (sem senha digitada)
- Você define um segredo em `ACCESS_TOKEN` (ex.: `kat-demo-9x2`).
- Você compartilha o link **com o token embutido**:
  `https://SEU-APP.onrender.com/?t=kat-demo-9x2`
- A pessoa **clica uma vez**: o servidor grava um cookie (válido 30 dias) e **remove o token da barra**.
  A partir daí ela abre o painel e usa a IA normalmente, **sem nunca digitar nada**.
- Quem não tem o link vê "acesso restrito". Isso evita que a sua chave seja usada por estranhos.
- Se você **não** definir `ACCESS_TOKEN`, o painel fica **totalmente aberto** (IA 100% nativa, sem
  qualquer barreira) — nesse caso, confie no limite de gasto + no limite de requisições abaixo.
- Proteção extra invisível: **limite de requisições por IP** (`RATE_MAX`, padrão 30/min).

## Publicar no Render (plano gratuito)

1. **Suba esta pasta `deploy/` para um repositório no GitHub:**
   ```bash
   cd "/Users/rafaeloliveira/CRM KAT/dashboards/deploy"
   git init && git add . && git commit -m "Painel KAT MVP"
   git remote add origin https://github.com/SEU_USUARIO/painel-kat.git
   git branch -M main && git push -u origin main
   ```
   > O `.gitignore` já impede que segredos subam. A chave **não** está em nenhum arquivo.

2. **No Render** (render.com) → **New +** → **Web Service** → conecte o repositório.
   - **Language:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python servidor.py`

3. Em **Environment**, adicione:
   | Chave | Valor | Obrigatório? |
   |---|---|---|
   | `ANTHROPIC_API_KEY` | a sua chave **nova** | sim (para a IA) |
   | `ACCESS_TOKEN` | um segredo p/ o link, ex.: `kat-demo-9x2` | opcional (recomendado) |
   | `MODELO` | modelo da IA — padrão `claude-haiku-4-5` (mais barato). Troque p/ `claude-sonnet-5` ou `claude-opus-4-8` se quiser | opcional |
   | `RATE_MAX` | ex.: `30` (perguntas de IA por minuto por IP) | opcional |

   > O Render define `PORT` sozinho.

4. **Create Web Service.** Em ~2 min você recebe a URL, ex.: `https://painel-kat.onrender.com`.

5. **Envie para a pessoa o link com o token:**
   `https://painel-kat.onrender.com/?t=kat-demo-9x2`
   (se você não usou `ACCESS_TOKEN`, é só a URL pura). Pronto — ela abre e a IA funciona.

### Plano gratuito do Render
- Hiberna após ~15 min sem uso; o primeiro acesso depois disso demora ~30s para "acordar".
- Para uma demo, abra o link 1 min antes para "aquecer".

## Alternativas equivalentes
- **Railway** (railway.app): Deploy from GitHub → mesmas variáveis de ambiente.
- **Replit**: importe a pasta, ponha as variáveis em *Secrets*, rode `python servidor.py`.

## Testar localmente antes de publicar
```bash
cd "/Users/rafaeloliveira/CRM KAT/dashboards/deploy"
ACCESS_TOKEN=demo123 ANTHROPIC_API_KEY=sua-chave-nova python3 servidor.py
# abra http://localhost:7799/?t=demo123
```

## Resumo de segurança
- Chave **só no servidor** (variável de ambiente) — nunca no navegador nem no repositório.
- Acesso pelo **token no link** (a pessoa não digita senha) ou aberto, você escolhe.
- **Limite de gasto** na chave + **limite de requisições** por IP contêm custos.
- Depois da demo: troque o `ACCESS_TOKEN` e/ou revogue a chave.
