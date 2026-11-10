# Migração de e-mail em lote com imapsync

`migrar_email.py` migra várias contas de e-mail entre dois servidores IMAP usando o
[imapsync](https://imapsync.lamiral.info/). Mostra o progresso por conta e o total, lista o que
falhou e grava um relatório ao final.

**Requisitos:** `imapsync` no PATH e Python 3, não há dependências Python para instalar.
Testado com Python 3.12 e imapsync 2.229, entre servidores Dovecot.

## Início rápido

```bash
./migrar_email.py
```

O script pergunta, nesta ordem: quantidade de contas, servidor de origem, servidor de destino e
as credenciais de cada conta. Depois testa os logins e começa a migração.

Sequência recomendada para uma migração real:

```bash
# 1 - Conta piloto, em modo simulação: nada é copiado
./migrar_email.py --csv piloto.csv --origem imap.antigo.com --destino imap.novo.com --simular

# 2 - Confira no log para qual pasta cada pasta iria
grep '^Folder ' $(ls -td execucoes/*/simulacoes/*/ | head -1)logs/*.txt

# 3 - Migração real
./migrar_email.py --csv contas.csv --origem imap.antigo.com --destino imap.novo.com --medir

# 4- Se algo falhou, depois de corrigir a causa
./migrar_email.py --repetir-falhas --csv contas.csv

```

Use as mesmas opções do imapsync em todas as rodadas (veja [Nomes de pasta](#nomes-de-pasta)).

## Argumentos

| Argumento | O que faz | Padrão |
|---|---|---|
| `-h`, `--help` | Mostra a ajuda e sai. | |
| `--origem HOST[:PORTA]` | Servidor de origem. | pergunta |
| `--destino HOST[:PORTA]` | Servidor de destino. | pergunta |
| `--csv ARQUIVO` | Lê as contas de um CSV, em vez de perguntar. | pergunta |
| `--repetir-falhas [RELATORIO]` | Repete só as contas que não terminaram OK. | última execução |
| `--paralelo N` | Quantas contas migrar ao mesmo tempo. | `1` |
| `--tentativas N` | Tentativas por conta em erros temporários. | `2` |
| `--simular` | Mostra o que seria feito, sem copiar nada. | desligado |
| `--medir` | Mede as contas antes, para a barra TOTAL andar por volume. | desligado |
| `--telegram` | Avisa no Telegram cada conta concluída e o resumo final. | desligado |
| `--sem-validacao` | Não testa os logins antes de começar. | valida |
| `--dir-execucoes DIR` | Onde gravar logs e relatórios. | `./execucoes` |
| `-- OPÇÕES` | Repassa o que vier depois ao imapsync. | |

### `--origem` e `--destino`

Aceitam `host`, `host:porta` ou, para IPv6, `[::1]:143`. Sem a porta, o imapsync escolhe a porta e
a criptografia sozinho.

```bash
./migrar_email.py --origem imap.antigo.com:993 --destino imap.novo.com
```

### `--csv`

O cabeçalho é obrigatório, e o separador pode ser `,` ou `;`. O separador é detectado pelo
cabeçalho, então senhas podem conter esses caracteres: em um arquivo separado por `;`, a senha pode
ter `,`, e vice-versa. Se a senha tiver o próprio separador ou aspas, coloque-a entre aspas.

```csv
usuario_origem;senha_origem;usuario_destino;senha_destino
joao@empresa.com;s3nha;joao@empresa.com;0utra
maria@empresa.com;s3nha2;;
```

- `usuario_origem` e `senha_origem` são obrigatórios.
- `usuario_destino` e `senha_destino` vazios (ou colunas ausentes) usam os mesmos valores da origem.

O arquivo guarda senhas em texto puro. Use `chmod 600 contas.csv` e apague-o ao terminar. O script
avisa se outros usuários puderem lê-lo.

### `--repetir-falhas`

Repete as contas com status diferente de `OK`, usando os servidores e as opções do imapsync gravados
no relatório. O que já foi copiado não é copiado de novo.

```bash
./migrar_email.py --repetir-falhas                         # última execução; pede as senhas
./migrar_email.py --repetir-falhas --csv contas.csv         # pega as senhas do CSV
./migrar_email.py --repetir-falhas execucoes/empresa.com/2026-11-05_103727
```

- O relatório nunca guarda senhas: sem `--csv`, elas são pedidas de novo.
- Se passar `--origem`, `--destino` ou `-- OPÇÕES`, eles substituem o que está no relatório.
- Se você usou outro `--dir-execucoes`, informe-o também, ou passe o caminho do relatório.
- Simulações não contam como "última execução".

### `--paralelo`

Aumenta a velocidade total, mas também a carga nos dois servidores. Provedores podem limitar conexões
simultâneas; comece com 2 ou 3.

### `--tentativas`

Tenta de novo, depois de 15 segundos, apenas em erros que podem ser temporários: conexão, erros
durante a sincronização e leitura na origem. Senha errada e quota excedida não são repetidas, porque
falhariam de novo.

### `--simular`

Roda o imapsync com `--dry`: mostra quantas mensagens seriam copiadas e para qual pasta cada pasta
iria, sem alterar nada.

### `--medir`

Sem esta opção, cada conta pesa igual na barra TOTAL. Com contas de 235 e 1200 mensagens, ela marca
50% ao terminar a primeira, embora só 16% das mensagens tenham sido processadas. Com `--medir`, o peso
de cada conta é o tamanho dela, e a linha TOTAL também mostra o volume (ex.: `1.2 MiB/2.5 MiB`), se
couber no terminal.

- A cópia não fica mais lenta: só há uma etapa a mais no início, que lê os tamanhos sem baixar
  mensagens e mede até 8 contas ao mesmo tempo. Nos testes locais, levou cerca de 1 segundo para 1200
  mensagens; em servidores remotos, deve levar mais.
- Vale a pena quando as contas têm tamanhos muito diferentes. Em contas parecidas, muda pouco.
- Conta que não puder ser medida entra com o tamanho médio das demais.
- Dentro de cada conta, o avanço continua sendo por mensagens. O volume exibido é uma estimativa.

### `--telegram`

Avisa no Telegram o início da migração, o resultado de cada conta e o resumo final, com a lista do que
não terminou OK. O resumo também é enviado quando a migração é interrompida.

```
✔ joao@empresa.com.br: 1234 copiada(s), 0 já existia(m), 450.0 MiB em 12:30
✖ maria@empresa.com.br: Falha de autenticação no destino (usuário/senha) | Host2 failure: ...
🏁 Migração concluída: imap.antigo.com → imap.novo.com
```

O token e o chat_id vêm de variáveis de ambiente, para não ficarem no `ps` nem no histórico do shell:

```bash
export TELEGRAM_TOKEN='123456789:AAH...'
export TELEGRAM_CHAT_ID='123456789'
./migrar_email.py --csv contas.csv --origem imap.antigo.com --destino imap.novo.com --telegram
```

Para criar o bot e descobrir o chat_id:

1. No Telegram, mande `/newbot` para o **@BotFather** e siga as instruções, ele devolve o token.
2. Abra a conversa com o seu bot, toque em **Iniciar** e mande qualquer mensagem.
3. Rode `curl -s "https://api.telegram.org/botSEU_TOKEN/getUpdates"` e copie o número em
   `"chat":{"id":...}`. Para avisar num grupo, adicione o bot ao grupo e mande uma mensagem lá
   antes; o chat_id de grupo começa com `-`.

- **O Telegram não interfere na migração:** se ele estiver fora do ar, a migração segue normalmente,
  e o script avisa no fim quantas mensagens não foram enviadas.
- **Simulações** chegam com o prefixo `[SIMULAÇÃO]`.
- **Privacidade:** os nomes das contas e as mensagens de erro ficam salvos no Telegram. Senhas nunca
  são enviadas.
- **Token:** quem tiver o token controla o bot. Se ele vazar, gere outro com `/revoke` no @BotFather.

### `--sem-validacao`

Pula o teste de login inicial. Por padrão, as contas são testadas antes, e o script oferece continuar
só com as válidas. As que falharam ficam no relatório como `FALHOU`, prontas para `--repetir-falhas`.

### `--dir-execucoes`

Muda a pasta onde ficam logs e relatórios. Veja [Arquivos gerados](#arquivos-gerados).

### `-- OPÇÕES` (opções do imapsync)

Tudo depois de `--` vai direto ao imapsync. Lista completa em `imapsync --help`. As mais úteis:

| Opção | O que faz |
|---|---|
| `--automap` | Associa pastas especiais (enviados, lixeira etc.) às equivalentes no destino. |
| `--f1f2 "A=B"` | Força a pasta `A` da origem a ir para a pasta `B` no destino. |
| `--exclude 'regex'` | Ignora as pastas que casarem com a expressão. |
| `--folder "INBOX"` | Sincroniza só essa pasta. |
| `--maxsize N` | Ignora mensagens com `N` bytes ou mais. |
| `--delete2` | **Apaga** do destino o que não existe na origem. Use com cuidado. |
| `--gmail1`, `--gmail2` | Origem (`1`) ou destino (`2`) é Gmail. |
| `--office1`, `--office2` | Origem ou destino é Microsoft 365. |
| `--exchange1`, `--exchange2` | Origem ou destino é Exchange. |

```bash
./migrar_email.py --csv contas.csv --origem a.com --destino b.com -- --automap --maxsize 50000000
```

Senhas não são aceitas aqui (`--password1` é recusado), porque ficariam visíveis no `ps` e gravadas no
relatório.

## Nomes de pasta

Por padrão, cada pasta é copiada com o nome que tem na origem. Se a origem usa `Sent` e o destino usa
`Enviados`, o destino fica com duas pastas, e os e-mails antigos não aparecem em "Enviados".

`--automap` resolve isso quando o servidor marca a pasta como especial (`\Sent`, `\Trash` etc.), ou
quando o nome está na lista interna do imapsync. **Essa lista não inclui `Enviados`, `Lixeira` nem
`Rascunhos`.** Nesses casos, use `--f1f2`.

| Cenário testado | Opção | Pasta no destino |
|---|---|---|
| `Sent` → `Enviados` marcada como `\Sent` | nenhuma | `Sent` (pasta separada) |
| `Sent` → `Enviados` marcada como `\Sent` | `--automap` | `Enviados` |
| `Sent` → `Enviados` sem marca | `--automap` | `Sent` (pasta separada) |
| `Enviados` sem marca → `Sent` marcada | `--automap` | `Enviados` (pasta separada) |
| `Sent` + `Sent Items` → `Enviados` marcada | `--automap` | `Enviados` (as duas juntas) |
| `Sent` → `Enviados` sem marca | `--f1f2 "Sent=Enviados"` | `Enviados` |

```bash
./migrar_email.py --simular -- --f1f2 "Sent=Enviados" --f1f2 "Trash=Lixeira"
```

- **Nenhum desses casos perdeu mensagens nem alterou a origem.** O risco é a mensagem ir para a pasta
  errada.
- **O status `OK` não confirma a pasta.** Confira o mapa no log com `grep '^Folder'`.
- **Use as mesmas opções em todas as rodadas.** Rodar sem `--automap` e depois com ele duplica no
  destino as mensagens das pastas associadas.

## Rodando mais de uma vez

Pode rodar quantas vezes precisar. O imapsync compara os cabeçalhos das mensagens e copia só o que
ainda não existe no destino. Se interromper, a próxima rodada continua de onde parou.

- Mensagem apagada na origem continua no destino, a menos que você use `--delete2`.
- Mensagem movida de pasta na origem é copiada para a pasta nova, e a cópia antiga fica no destino.
- A nova rodada precisa comparar os cabeçalhos, então não é instantânea em caixas grandes.

## O painel de progresso

```
 ✔ pedro@ex.com → pedro.int@ex.com: 235 copiada(s), 0 já existia(m), 493.4 KiB em 00:02
 TOTAL   ███████████████░░░░░░░░░  62.3%  1/2 contas  ok 1  erros 0  falhas 0  00:04  ETA 00:03
  grande@ex.com   ███████████░░░░░░░░░░░░░  47.5%  570/1200 msgs  INBOX (3/6 pastas)  338 KiB/s
```

- **Linhas `✔`, `⚠` e `✖`:** resultado de cada conta, no momento em que termina.
- **`TOTAL`:** progresso geral. Por conta, ou por volume com `--medir`.
- **Linha da conta:** mensagens processadas, pasta atual, velocidade e tempo restante.

Em terminais estreitos, volume, tempo restante e velocidade podem ser omitidos ou cortados; os
contadores da linha TOTAL ficam sempre visíveis. Com a saída redirecionada para um arquivo, o script
imprime uma linha a cada conta concluída e a cada 30 segundos.

**Ctrl-C** encerra o imapsync, grava o relatório e mostra o resumo. Depois é só usar `--repetir-falhas`.

## Status das contas

| Status | Significado |
|---|---|
| `OK` | Terminou sem erros, e todas as mensagens da origem estão no destino. |
| `COM_ERROS` | Terminou, mas com problemas: mensagens recusadas, quota, pasta inacessível etc. |
| `FALHOU` | Não conseguiu migrar: login, conexão, TLS etc. |
| `INTERROMPIDA` | Parada no meio (Ctrl-C, por exemplo). |
| `NAO_EXECUTADA` | Não chegou a começar. |

## Arquivos gerados

```
execucoes/
├── ultima -> empresa.com/2026-11-05_103727    atalho para a última execução real
└── empresa.com/                                domínio das contas de origem
    ├── simulacoes/
    │   └── 2026-11-05_101500/                  execuções com --simular
    └── 2026-11-05_103727/
        ├── relatorio.csv                       uma linha por conta
        ├── relatorio.json                      o mesmo, com todos os erros e as opções usadas
        └── logs/
            ├── 001_joao@empresa.com.txt             log completo do imapsync
            ├── 001_joao@empresa.com_tentativa2.txt  nova tentativa, se houve
            ├── 001_joao@empresa.com.validacao.txt   teste de login
            └── 001_joao@empresa.com.medicao.txt     medição (--medir)
```

A pasta do domínio vem das contas de origem. Com vários domínios, ela se chama
`empresa.com_e_mais_2`; sem domínio no usuário, `sem_dominio`.

O relatório é atualizado a cada conta concluída. As pastas de execução só podem ser lidas pelo dono.
Senhas não são gravadas nos logs nem nos relatórios.

Colunas do relatório:

| Coluna | Significado |
|---|---|
| `usuario_origem`, `usuario_destino` | A conta. |
| `status`, `motivo` | Resultado e explicação. |
| `codigo_saida` | Código de saída do imapsync. |
| `copiadas` | Mensagens copiadas nesta execução (em `--simular`, as que seriam copiadas). |
| `ja_existiam` | Mensagens que já estavam no destino. |
| `faltando_no_destino` | Mensagens da origem que não estão no destino. |
| `bytes_copiados`, `copiado_legivel` | Volume copiado, em bytes e legível (ex.: `493.4 KiB`). |
| `tamanho_origem_bytes`, `tamanho_origem_legivel` | Tamanho medido, em bytes e legível; vazio sem `--medir`. |
| `mensagens_origem` | Quantidade de mensagens medida; vazio sem `--medir`. |
| `tentativas`, `duracao_s` | Tentativas usadas e tempo gasto. |
| `log` | Caminho do log. |
| `primeiro_erro` (CSV) ou `erros` (JSON) | Mensagem de erro do imapsync. |

## Códigos de saída do script

| Código | Significado |
|---|---|
| `0` | Todas as contas terminaram OK, ou não havia nada para repetir. |
| `1` | Alguma conta não terminou OK. |
| `2` | Entrada inválida, ou imapsync não encontrado. |
| `130` | Cancelado ou interrompido (Ctrl-C, sinal de encerramento). |

Isso permite usar o script em automações, como cron. Sem terminal para responder, o script segue com
as contas válidas quando alguma falha na validação de login.

## Problemas comuns

| Mensagem | O que verificar |
|---|---|
| Falha de autenticação na origem/destino | Usuário e senha. Alguns provedores exigem senha de app ou IMAP habilitado. |
| Não foi possível conectar | Host, porta, firewall e se o servidor está no ar. |
| Destino sem espaço (quota excedida) | Aumente a quota do destino e rode de novo. |
| Destino recusou mensagens (APPEND) | Costuma ser mensagem acima do limite do destino. Veja `--maxsize`. |
| N mensagens da origem não estão no destino | Veja os erros no log da conta e rode de novo. |

Para qualquer falha, o log da conta (coluna `log` do relatório) traz a saída completa do imapsync.
