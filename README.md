# Gestor de Apostas (bot de Telegram)

Bot de Telegram pra registrar apostas esportivas e acompanhar resultados, com os dados num Postgres.

## O que ele faz

- **Nova aposta:** pergunta data, horário, descrição, odd, stake, esporte e casa, e salva.
- **Ver pendentes:** lista as apostas sem resultado. Marque rápido mandando `green 12`, `red 12 13` ou `void 14`.
- **Resultados:** lucro, ROI e estatísticas gerais, por casa, por esporte, por mês, por semana e por dia.
- **Editar aposta:** pelo menu ou mandando `editar 12`. Também aceita `duplicar 12`, `triplicar 12`, `duplicar 12 casa Bet365` e `deletar 12 13`.
- **Gerar Resultados:** planilha `.xlsx` e dashboard em HTML. `/exportar` manda tudo em CSV.
- **Configurações:** valor da unidade, gastos e casas limitadas (que somem da lista de casas).
- **Modo IA** (botão no menu principal): com ele ligado, encaminhe o print de uma aposta e o Claude lê os dados. O bot mostra o que leu e só salva se você confirmar. Precisa de `ANTHROPIC_API_KEY`.

As tabelas (`apostas`, `configuracoes`, `gastos`, `casas_limitadas`) são criadas sozinhas na primeira vez que o bot roda.

## Variáveis de ambiente

No Render, ficam em **Environment → Environment Variables**. No computador, use um arquivo `.env` ou `set`/`export` (veja o `.env.example`). Nunca coloque esses valores no código nem suba o `.env` pro GitHub.

| Variável | Obrigatória | Valor |
|---|---|---|
| `TELEGRAM_TOKEN` | sim | Token do bot, que você pega com o @BotFather (`/newbot` ou `/token`). O nome antigo `TOKEN` também funciona. |
| `DATABASE_URL` | sim | URL do Postgres. No Render, use a **Internal Database URL** (veja abaixo). |
| `ANTHROPIC_API_KEY` | não | Chave da API da Anthropic, criada em console.anthropic.com. Só é usada pelo Modo IA. Sem ela, o resto do bot funciona normal. |
| `PYTHON_VERSION` | recomendada | Versão do Python que o Render instala. Use `3.11.9`, a mesma do `runtime.txt`. |
| `TZ` | recomendada | Fuso horário do servidor. Use `America/Sao_Paulo` (horário de Brasília). |
| `PORT` | não | O Render define sozinho. É a porta do servidor de ping. |

### Por que o `TZ` importa

Os servidores do Render rodam em UTC, 3 horas à frente de Brasília. O bot usa a hora do servidor pra preencher a data e o horário de uma aposta nova e pra separar os resultados por dia, semana e mês. Sem `TZ=America/Sao_Paulo`, uma aposta feita às 22h aparece no dia seguinte.

### Internal ou External Database URL

O Postgres do Render tem duas URLs, as duas na aba **Connections** do banco:

- **Internal Database URL:** só funciona de dentro do Render. É a que vai na variável `DATABASE_URL` do web service do bot. Ela é mais rápida e não sai pra internet.
- **External Database URL:** funciona de qualquer lugar. É a que você usa quando o código Python roda **fora** do Render: no seu computador (`python bot_apostas.py` local) e no script de migração `migrar_db.py`, no campo `NEW_URL`.

Se você colocar a External no Render, funciona, mas passa pela internet à toa. Se usar a Internal no seu computador, a conexão falha, porque esse endereço não existe fora do Render.

## Subir no Render

1. **Banco:** no Render, vá em **New → PostgreSQL**, dê um nome e crie. Na aba **Connections**, copie a **Internal Database URL**.
2. **Bot:** vá em **New → Web Service** e conecte este repositório.
   - Runtime: Python 3
   - Build command: `pip install -r requirements.txt`
   - Start command: `python bot_apostas.py` (o mesmo do `Procfile`)
3. Em **Environment**, crie as variáveis:
   - `TELEGRAM_TOKEN` = token do BotFather
   - `DATABASE_URL` = a Internal Database URL do passo 1
   - `PYTHON_VERSION` = `3.11.9`
   - `TZ` = `America/Sao_Paulo`
   - `ANTHROPIC_API_KEY` = sua chave, se for usar o Modo IA
4. Clique em **Deploy**. No Telegram, mande `/start` pro bot.

Se faltar `TELEGRAM_TOKEN` ou `DATABASE_URL`, o bot para logo no início e o log do Render mostra qual variável está faltando.

### Plano grátis

O web service grátis hiberna depois de 15 minutos sem tráfego HTTP, e aí o bot para de responder. O bot sobe um servidor que responde `OK` em `/`, então dá pra mantê-lo acordado com um monitor de ping externo. Fora isso, o jeito é o plano pago.

O Postgres grátis do Render expira depois de 30 dias. Antes disso, use **Gerar Resultados → Gerar Dados**. O bot gera um `migrar_db.py` com todos os seus dados. Crie um banco novo, coloque a **External Database URL** dele no `NEW_URL` do script e rode no seu computador. Depois troque a `DATABASE_URL` do web service pela **Internal Database URL** do banco novo. O `migrar_db.py` tem seus dados, então não suba ele pro GitHub (ele já está no `.gitignore`).

## Rodar no computador

```bash
pip install -r requirements.txt
```

Use a **External Database URL** aqui:

```bash
export TELEGRAM_TOKEN=... DATABASE_URL=postgresql://... TZ=America/Sao_Paulo
python bot_apostas.py
```

No Windows (CMD), troque o `export` por `set TELEGRAM_TOKEN=...` e `set DATABASE_URL=...`, um por linha. O Windows já usa o fuso do seu computador, então o `TZ` não é necessário lá.

Não rode o bot no computador e no Render ao mesmo tempo com o mesmo token. O Telegram só entrega as mensagens pra um deles, e os dois ficam dando erro de conflito.
