# Gestor de Apostas (bot de Telegram)

Bot de Telegram pra registrar apostas esportivas e acompanhar resultados, com os dados num Postgres.

## O que ele faz

- **📝 Nova aposta:** pergunta data, horário, descrição, odd, stake, esporte e casa, e salva.
- **⏳ Ver pendentes:** lista as apostas sem resultado. Marque rápido mandando `green 12`, `red 12 13` ou `void 14`.
- **📈 Resultados:** lucro, ROI e estatísticas gerais, por casa, por esporte, por mês, por semana e por dia.
- **✏️ Editar aposta:** pelo menu ou mandando `editar 12`. Também aceita `duplicar 12`, `triplicar 12`, `duplicar 12 casa Bet365` e `deletar 12 13`.
- **📊 Gerar Resultados:** planilha `.xlsx` e dashboard em HTML. `/exportar` manda tudo em CSV.
- **⚙️ Configurações:** valor da unidade, gastos e casas limitadas (que somem da lista de casas).
- **Modo IA** (botão no menu principal): com ele ligado, encaminhe o print de uma aposta e o Claude lê os dados. O bot mostra o que leu e só salva se você confirmar. Precisa de `ANTHROPIC_API_KEY`.

As tabelas (`apostas`, `configuracoes`, `gastos`, `casas_limitadas`) são criadas sozinhas na primeira vez que o bot roda.

## Variáveis de ambiente

| Variável | Obrigatória | O que é |
|---|---|---|
| `TOKEN` | sim | Token do bot, que você pega com o @BotFather |
| `DATABASE_URL` | sim | URL do Postgres (no Render, use a *Internal Database URL*) |
| `ANTHROPIC_API_KEY` | não | Só pro Modo IA |
| `PORT` | não | O Render define sozinho. É a porta do servidor de "ping" |

Nunca coloque essas informações no código. Veja o `.env.example`.

## Subir no Render

1. **Banco:** no Render, vá em **New → PostgreSQL**, dê um nome e crie. Depois copie a **Internal Database URL** na aba *Connections*.
2. **Bot:** vá em **New → Web Service** e conecte este repositório.
   - Runtime: Python 3 (a versão está em `runtime.txt`)
   - Build command: `pip install -r requirements.txt`
   - Start command: `python bot_apostas.py` (o mesmo do `Procfile`)
3. Em **Environment**, crie `TOKEN`, `DATABASE_URL` (a URL interna do passo 1) e, se for usar, `ANTHROPIC_API_KEY`.
4. Clique em **Deploy**. No Telegram, mande `/start` pro bot.

**Sobre o plano grátis:** o web service grátis hiberna depois de 15 minutos sem tráfego HTTP, e o bot para de responder. O bot sobe um servidor que responde `OK` em `/`, então dá pra mantê-lo acordado com um monitor de ping externo. Fora isso, o jeito é o plano pago. O Postgres grátis do Render expira depois de 30 dias. Antes disso, use **📊 Gerar Resultados → 📂 Gerar Dados**: ele gera um `migrar_db.py` com todos os dados pra você copiar pra um banco novo.

## Rodar no computador

```bash
pip install -r requirements.txt
```

```bash
export TOKEN=... DATABASE_URL=postgresql://...
python bot_apostas.py
```

No Windows (CMD), troque o `export` por `set TOKEN=...` e `set DATABASE_URL=...`.
