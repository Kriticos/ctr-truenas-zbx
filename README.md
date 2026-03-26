# CTR TrueNAS Zabbix Collector

Automação para coletar métricas de TrueNAS via API WebSocket e enviar para Zabbix via `zabbix_sender`.

## 🔍 Objetivo

- Conectar em um TrueNAS/TrueNAS SCALE via WebSocket (`wss://<host>/api/current`).
- Autenticar com *API Key* (`auth.login_with_api_key`).
- Coletar:
  - versão do sistema
  - hostname
  - pools (total/used/free)
  - discos (nome/modelo/serial/temperatura)
  - datasets raiz (usado/livre)
- Enviar payload JSON completo para Zabbix pelo `zabbix_sender` usando uma key configurável.

## 📦 Estrutura do projeto

- `docker-compose.yml` - serviço `ctr-truenas-zbx` usando `python:3.12-slim`.
- `app/requirements.txt` - dependências Python:
  - `websocket-client`
  - `requests`
- `app/truenas_zbx.py` - script principal.
- `.env` - variáveis de ambiente para o container.
- `logs/` - volume de logs (não usado pelo script mas preserva no host).

## ⚙️ Variáveis de ambiente (requeridas)
Defina em `.env` ou via `docker compose`:

- `TRUENAS_HOST` (ex: `192.168.0.21`)
- `TRUENAS_API_KEY` (API key de TrueNAS)
- `ZABBIX_SERVER` (ex: `192.168.0.20`)
- `ZABBIX_HOST` (nome do host no Zabbix)

Variáveis opcionais com valores padrão:

- `TRUENAS_WS_PATH` = `/api/current`
- `TRUENAS_VERIFY_SSL` = `true` (set `false` para certs autofirmados temporariamente)
- `ZABBIX_PORT` = `10051`
- `ZABBIX_KEY` = `truenas.raw`
- `COLLECT_INTERVAL` = `300` (segundos)

## ▶️ Como executar

```bash
docker compose down
docker compose up -d --build
```

Ver logs:

```bash
docker logs -f ctr-truenas-zbx
```

## ✅ Comportamento esperado

- Ao iniciar, o script faz `pip install -r requirements.txt` (container rebuild já instala) e roda `python /app/truenas_zbx.py`.
- Faz a coleta via RPC TrueNAS e imprime JSON final.
- Envia para Zabbix com `zabbix_sender`.
- Pausa por `COLLECT_INTERVAL` segundos e repete.
- Em erro, loga o erro e continua no próximo ciclo.

## 🛠️ Verificações rápidas

1. `TRUENAS_HOST` e `TRUENAS_API_KEY` configurados.
2. TrueNAS acessível e responde em `wss://<TRUENAS_HOST>/api/current`.
3. `zabbix_sender` instalado no container (image base precisa incluir pacote).  
   - No Dockerfile: `apt-get install -y zabbix-sender` se ainda não estiver.
4. `ZABBIX_HOST` exista no Zabbix.

## 🐞 Diagnóstico do problema inicial do container

O container estava reiniciando rapidamente porque o `app/truenas_zbx.py` estava vazio. Nesse caso, o processo terminava com exit code 0 e o `restart: unless-stopped` provocava loop.  
Agora o script está populado e funcionando (logs mostram autenticação, coleta e envio OK).

## 📌 Possíveis melhorias

- tratar `zabbix_sender` não instalado com falha clara.
- converter para Dockerfile customizado com dependências e `zabbix-sender` instalados.
- adicionar `HEALTHCHECK` no `docker-compose`.
- adicionar tradução/labels em inglês caso o time seja bilíngue.

---

`Curso de uso rápido`

1. Ajuste `.env`.
2. `docker compose up -d --build`
3. `docker logs -f ctr-truenas-zbx`
4. Conferir no Zabbix as entradas da key configurada.
