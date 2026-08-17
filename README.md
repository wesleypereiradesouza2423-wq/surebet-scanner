# Surebet Server V1

Servidor 24/7 do Surebet Scanner.

- FastAPI + Gunicorn
- PostgreSQL persistente
- coleta automática
- mercados h2h de 2 e 3 resultados
- revalidação de candidatos antes de gravar
- painel web responsivo

## Variáveis do Railway

Nunca coloque a chave real no repositório. Configure no serviço:

- `ODDS_API_KEY` = sua chave da The Odds API
- `DATABASE_URL` = referência ao PostgreSQL do Railway
- `SCAN_INTERVAL_SECONDS=180`
- `MIN_ROI=0.50`
- `MAX_SPORTS=15`
- `REGIONS=eu,uk`
- `MAX_ODD_AGE_SECONDS=90`
- `BOOKS=Pinnacle,Betfair,Betano,Unibet,Betclic,William Hill,Bet Victor,Betsson,1xBet,Matchbook`

O serviço usa um único worker porque o coletor 24/7 roda dentro do processo da aplicação.
