# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio em Python, com o SDK oficial do MCP na v2 (`mcp==2.3.0`, revisão `2026-07-28` da spec) e A2A v1.0 no binding JSON-RPC 2.0 sobre HTTP. São dois processos separados que só conversam por HTTP:

| Pasta | Processo | Porta | Papel |
|---|---|---|---|
| `servidor-mcp/` | `servidor-mcp` | 7301, endpoint `/mcp` | Servidor MCP `central-de-salas`, Streamable HTTP stateless, três tools, um resource e o MRTR na reserva |
| `agente/` | `agente` | 7300, endpoints `/a2a` e `/.well-known/agent-card.json` | Host MCP por dentro, servidor A2A por fora, sem LLM |

`dados/`, `validador/` e `exemplos/` estão como vieram do starter.

## Como rodar

Precisa de Python 3.10 ou superior (testado com 3.12 e 3.14). A partir de um clone limpo, instale os dois processos num ambiente virtual só. O `constraints.txt` trava a versão exata de todas as dependências, inclusive as transitivas; as diretas também estão travadas no `pyproject.toml` de cada processo.

```bash
git clone https://github.com/camillacorreia/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
python3 -m venv .venv
.venv/bin/pip install -c constraints.txt -e ./servidor-mcp -e ./agente
```

O servidor MCP precisa da chave de integridade do `requestState` na variável `REQUEST_STATE_SECRET`, com no mínimo 32 bytes de aleatoriedade. Não existe valor padrão: sem a variável, ou com uma chave curta, o processo para na subida com uma mensagem dizendo como gerar uma. Gere a sua, nunca reutilize uma publicada e não a coloque no repositório.

Terminal 1, servidor MCP (o stderr fica visível nesse terminal):

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
.venv/bin/servidor-mcp
```

Para guardar o log num arquivo sem tirá-lo da tela, use `.venv/bin/servidor-mcp 2>&1 | tee mcp.log` (arquivos `*.log` estão no `.gitignore`). Para o teste de restart do `requestState`, pare com Ctrl+C e suba de novo no mesmo terminal: a variável continua exportada e a chave é a mesma.

Terminal 2, agente:

```bash
.venv/bin/agente
```

Terminal 3, validador, com os dois processos recém-iniciados:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

As portas e endereços podem ser trocados por variável de ambiente, e os padrões são os que o validador espera:

- `MCP_HOST` e `MCP_PORT` no servidor MCP (padrão `127.0.0.1` e `7301`).
- `AGENTE_HOST` e `AGENTE_PORT` no agente (padrão `127.0.0.1` e `7300`).
- `MCP_URL` no agente, o servidor MCP que ele consome (padrão `http://127.0.0.1:7301/mcp`).
- `AGENTE_URL_PUBLICA` no agente, a URL anunciada no Agent Card (padrão `http://<AGENTE_HOST>:<AGENTE_PORT>/a2a`).
- `DADOS_DIR` no servidor MCP, se os JSON não estiverem em `dados/` na raiz do repositório.

O servidor escreve uma linha no stderr por request recebido, com método, id de JSON-RPC, nome da tool ou URI, `traceparent` do `_meta`, status HTTP e o tipo de resultado:

```
2026-11-17T16:35:41.362 mcp method=tools/list id=2 traceparent=00-dbbe7c0a25bb5f1838c4026f73c50ea4-2e7b626d4007646a-01 http=200 resultado=complete
2026-11-17T16:35:41.399 mcp method=tools/call id=7 name=reservar_sala traceparent=00-dbbe7c0a25bb5f1838c4026f73c50ea4-2204d57377b31552-01 http=200 resultado=input_required
2026-11-17T16:35:41.403 mcp method=tools/call id=8 name=reservar_sala traceparent=00-dbbe7c0a25bb5f1838c4026f73c50ea4-c401cf29d1493154-01 retry=sim http=200 resultado=complete
```

Os requests do agente têm id inteiro (os do validador são hexadecimais), então `grep "id=[0-9]" mcp.log` mostra só o agente: o primeiro é um `server/discover`, depois vem o `tools/list` antes de qualquer `tools/call`, e todo request de uma Task carrega o trace-id que chegou no header `traceparent` do A2A. O par de uma reserva pausada aparece como acima, com o retry marcado `retry=sim` e um id diferente do pedido inicial. Quando um `requestState` é recusado, o SDK registra o motivo real na linha anterior (`requestState rejected on tools/call: seal`, `expired`, `request binding` ou `unknown key`), enquanto o cliente recebe só o `-32602` genérico.

## Onde a ponte acontece

A ponte está em `agente/agente/tarefas.py`. Na ida, `Agente._concluir_rodada` recebe o resultado do `tools/call` feito por `HostMCP.chamar_tool` (`agente/agente/host_mcp.py`); quando ele é um `InputRequiredResult`, lê a chave e o `enum` da elicitation, guarda numa `Pausa` ligada à Task o nome da tool, os argumentos, a chave, as alternativas e o `requestState` exatamente como veio, e coloca a Task em `TASK_STATE_INPUT_REQUIRED` com a mensagem `alternativas: <ids>`. Na volta, `Agente._retomar` traduz `escolha=<id>` em `ElicitResult(action="accept", content={"sala": <id>})` e `escolha=recusar` em `action="decline"`, e chama de novo `HostMCP.chamar_tool` com a mesma tool e os mesmos argumentos, `inputResponses` com a mesma chave e `request_state=pausa.request_state`; é nesse ponto que o `requestState` volta ao servidor, ecoado byte a byte por `ClientSession.call_tool` do SDK, num request com id de JSON-RPC novo. Do lado do servidor, quem faz a pergunta é `_perguntar` e quem lê a resposta é `_retomar`, os dois em `servidor-mcp/servidor_mcp/servidor.py`.

## Decisões técnicas

### MRTR no servidor: `InputRequiredResult` do SDK com as alternativas seladas

Quando a sala está ocupada, o corpo do `reservar_sala` devolve um `InputRequiredResult` do SDK (`_perguntar` em `servidor_mcp/servidor.py`): uma única entrada em `inputRequests`, na chave `escolha_de_sala`, com um `ElicitRequest` em form mode cujo schema plano é gerado pelo `render_elicitation_schema` do SDK a partir de um modelo com `sala: Literal[<alternativas>]` (`enum`, ou `const` quando só existe uma alternativa). O `requestState` que sai do corpo da tool é um JSON com o pedido inteiro (sala, início, fim, responsável) e as alternativas oferecidas; o SDK o sela antes de ele chegar ao fio e, no retry, verifica o selo e entrega o texto puro em `Context.request_state`. Sem nenhuma alternativa, a tool devolve `isError` com `Sem alternativas disponiveis no intervalo`. Sem `{"elicitation": {"form": {}}}` no `_meta` do request, o servidor responde `-32021` com `data.requiredCapabilities` e HTTP 400, com a mesma regra e o mesmo formato que o SDK usa para resolvers. Os `-32602` com HTTP 400 para `_meta` incompleto saem do transporte do SDK (`classify_inbound_request` em `mcp/shared/inbound.py`) antes de chegar a qualquer handler, sem olhar requests anteriores.

No retry (`_retomar`), o servidor usa os valores selados, nunca os reenviados. `decline` e `cancel` concluem com `reservado: false` e `motivo: "recusado"`, sem `isError`. `accept` só vale para uma sala que estava entre as alternativas seladas; fora delas, a tool devolve `isError` com `Escolha invalida: ...`. Se a sala escolhida continua livre no intervalo selado (e atende à regra de capacidade), a reserva é concluída, mesmo que o servidor tenha sido reiniciado e as outras alternativas tenham mudado. Só quando a sala escolhida foi tomada entre a pausa e o retry o servidor pergunta de novo, com as alternativas calculadas agora e um `requestState` novo (ou `Sem alternativas disponiveis no intervalo`, se não sobrou nenhuma). O agente trata esse segundo `input_required` como o primeiro: a Task volta a pausar com a lista nova.

A primeira versão usava o resolver do SDK (`Resolve(...)` devolvendo `Elicit`), e a troca tem um motivo concreto. O estado que o SDK sela para resolvers guarda só as respostas e o digest da pergunta (`_State` em `mcp/server/mcpserver/resolve.py`), e a resposta só é aceita se o resolver, rodando de novo com os dados atuais, renderizar exatamente a mesma pergunta:

```python
answer = res.answers.get(key)
# An answer counts only for the rendering recorded when it was asked; an answer to
# an unrecorded or differently-worded question re-asks instead of being consumed.
if answer is not None and res.asked.get(key) != q:
```

Como as reservas vivem em memória, um restart do servidor entre a pausa e o retry muda a lista de alternativas, e uma escolha válida era descartada e perguntada de novo. Selar as alternativas oferecidas no próprio `requestState` resolve isso sem guardar nada no servidor, e o canal usado continua sendo o do SDK: o corpo da tool devolvendo `InputRequiredResult` é um fluxo suportado por ele (`convert_result` em `func_metadata.py` deixa o resultado passar como `resultType: "input_required"` e tira esse braço do `outputSchema`), e o selo continua sendo o `RequestStateBoundary`.

### Como o `requestState` é protegido e quanto tempo vale

A proteção é o utilitário do próprio SDK. O servidor é criado com `MCPServer(..., request_state_security=RequestStateSecurity(keys=[segredo], ttl=600))`, e o `MCPServer` instala o `RequestStateBoundary` (`mcp/server/request_state.py`), que sela todo `requestState` que sai e verifica todo que volta, antes de qualquer handler. O codec é o `AESGCMRequestStateCodec`: AES-256-GCM com chave derivada por HKDF-SHA256 do segredo, token no formato `v1.<base64url(kid, nonce, ciphertext, tag)>`. É AEAD, então além de detectar adulteração o conteúdo vai cifrado e o cliente não consegue lê-lo.

Dentro do envelope selado vão `iat` e `exp` (10 minutos, dentro da faixa de 5 a 30 pedida), o método, o nome da tool, um digest SHA-256 dos argumentos, a audiência (`central-de-salas`) e o estado da tool (o pedido inteiro e as alternativas oferecidas). Trocar qualquer caractere quebra a tag do GCM e o request recebe `-32602` com a mensagem `Invalid or expired requestState`; o mesmo acontece com token expirado, token selado por outra chave e token apresentado com outros argumentos ou outra tool.

O segredo vem só de `REQUEST_STATE_SECRET` (`servidor_mcp/__main__.py`, `ler_segredo`). Um valor em hexadecimal, como o de `secrets.token_hex(32)`, vale pelos bytes que codifica, e o mínimo é 32. Não há chave no código nem valor padrão.

### O servidor não guarda nada entre a pergunta e o retry

Tudo que o servidor precisa para concluir o pedido viaja no `requestState`: sala, intervalo, responsável e as alternativas oferecidas. O retry é concluído com esses valores selados. Além disso, o `RequestStateBoundary` exige que os argumentos reenviados batam com o digest selado, então argumentos divergentes no retry não tomam efeito, porque o estado é rejeitado inteiro com `-32602` (o caminho "o servidor rejeita o estado", aceito pelo enunciado e coberto pela verificação 18). Como a chave vem do ambiente e não do processo, um `requestState` emitido antes de um restart continua válido depois dele. Isso foi testado no fluxo do avaliador: depois de reservar `sala-mirante` pelo agente, o corpo de `exemplos/wire/03` recebe só `sala-fusca` como alternativa; com o servidor reiniciado (reservas de volta ao arquivo, alternativas atuais `sala-fusca, sala-mirante`), o retry de `exemplos/wire/04` aceitando `sala-fusca` conclui a reserva.

As reservas ficam em memória no servidor (`servidor_mcp/dominio.py`, `CentralDeSalas`), carregadas de `dados/` na subida e numeradas a partir de `res-0003`. A ordem das validações é sala, intervalo invertido, janela e duração, para que cada caso caia na mensagem certa: `10:00 -> 09:00` é intervalo inválido, `07:00 -> 08:00` é janela e `09:00 -> 12:00` é duração.

### Onde fica o estado das Tasks

No agente, em memória: o dicionário `Agente.tarefas` em `agente/agente/tarefas.py`, uma `Tarefa` por id, com estado, histórico, artifacts, a versão da política lida do resource, o trace-id e, quando pausada, a `Pausa`. Cada Task tem a sua `Pausa` e a sua trava, então duas Tasks pausadas ao mesmo tempo não trocam de `requestState`, e duas continuações da mesma Task não correm em paralelo. A `Pausa` nunca entra na serialização A2A (`Tarefa.como_a2a`), por isso o `requestState` não aparece no card, nos artifacts nem nas mensagens. Estados terminais são definitivos: `SendMessage` numa Task `COMPLETED`, `CANCELED` ou `FAILED` recebe o erro `-32004` (UnsupportedOperation), Task desconhecida recebe `-32001` (TaskNotFound) e método desconhecido recebe `-32601`. Um restart do agente perde as Tasks, o que está fora do escopo pedido.

### O agente como cliente MCP

O agente usa o cliente do SDK, `ClientSession` sobre `streamable_http_client`, e chama `call_tool(..., allow_input_required=True)`. Com isso o `input_required` volta cru para o agente, e nenhum callback de elicitation é registrado, então nada responde a pergunta sozinho. O SDK carimba em todo request `protocolVersion`, `clientInfo` (`agente-central-de-salas` 1.0.0) e `clientCapabilities` no `_meta`, os headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name`, e gera um id novo por request, o que garante que o retry nunca repete o id do pedido inicial. O agente acrescenta o `traceparent` no `_meta` de cada request.

A cada Task o agente faz `tools/list` e confere que `reservar_sala` existe antes de chamar, e lê `politica://uso` para extrair a versão da primeira linha, que vai no artifact. A versão do protocolo é negociada uma vez por processo com `server/discover`, na primeira Task, para que o agente possa subir antes do servidor MCP.

O trace-id do header `traceparent` da chamada A2A é guardado na Task e usado em todos os requests MCP dela, inclusive no retry disparado por uma continuação que chega sem o header; cada request MCP ganha um span-id novo. Sem header, o agente gera um trace-id para a Task.

### Limitação do SDK encontrada

No `mcp==2.3.0` o cliente só anuncia elicitation quando existe um `elicitation_callback`, e nesse caso anuncia form e url juntos. Trecho de `mcp/client/session.py`, `ClientSession._build_capabilities`:

```python
elicitation = (
    types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
    if self._elicitation_callback is not _default_elicitation_callback
    else None
)
```

E o carimbo do `_meta` sobrescreve a capability por request, então ela também não pode ser passada no `meta` da chamada (`_make_modern_stamp`, `meta[CLIENT_CAPABILITIES_META_KEY] = capabilities`). O agente precisa declarar elicitation em form mode sem registrar callback (o callback fecharia o ciclo sem passar pelo cliente A2A) e não trata url mode. A saída mínima foi a subclasse `SessaoElicitacaoForm` em `agente/agente/host_mcp.py`, que sobrescreve `_build_capabilities` para anunciar exatamente `{"elicitation": {"form": {}}}`. É um método privado, por isso a versão do SDK está travada.

Duas diferenças do SDK que o enunciado já aceita: a mensagem dos erros de execução vem prefixada (`Error executing tool reservar_sala: Sala inexistente: ...`), e tool desconhecida volta como `isError: true` em vez de `-32602`. O agente repassa o texto da tool sem editar, então a Task `FAILED` mostra a mensagem com o prefixo.

### O lado A2A

O A2A foi escrito direto em Starlette (`agente/agente/a2a.py`), sem SDK de A2A, para que o formato no fio seja exatamente o de `exemplos/wire/07` a `10`: `supportedInterfaces` com `protocolBinding: JSONRPC` e `protocolVersion: 1.0`, resultados `{"task": {...}}`, papéis `ROLE_USER`/`ROLE_AGENT`, estados `TASK_STATE_*` e partes `{"text": ...}`. Uma Task nova passa por `SUBMITTED` e `WORKING` antes do estado final, e `GetTask` lê o estado corrente a qualquer momento. O pedido é lido por expressão regular no formato fixo (`agente/agente/pedido.py`); texto fora do formato termina a Task em `FAILED` com o formato esperado na mensagem. Sem LLM e sem aleatoriedade na decisão, o mesmo pedido produz sempre a mesma resposta.

## Saída do validador

Execução com os dois processos recém-iniciados:

```
trace-id desta execucao: 14ddb3d2776799558ffd70c004797f23
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
