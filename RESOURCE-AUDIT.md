# Auditoria de recursos — 2026-09-27

Foram corrigidos dez problemas do projeto, em dez commits independentes, sem push.
As alterações que já estavam no diretório e as cinco exclusões previamente
staged foram preservadas. A validação abaixo usa o diretório de trabalho completo,
incluindo essas alterações anteriores; não representa um checkout limpo de HEAD.

## Correções e regressões

| Commit | Problema corrigido | Prova mantida no projeto |
|---|---|---|
| `686297b` | Callbacks mantinham o diálogo de boas-vindas vivo | Finalização nativa após fechar pelo botão |
| `9138dc0` | Editor de segmentos e linhas removidas retidos por callbacks | Censo de editor e botões das linhas |
| `c6c830b` | Linhas da fila e menus criados sob demanda formavam ciclos | Censo com e sem abrir o menu |
| `dfc47af` | DrawingArea e controllers retinham waveform/seekbar | Censo dos dois widgets |
| `190076b` | Diálogo de resultados retinha o controlador | Censo após fechar e liberação da referência |
| `1a5b8e4` | Janela fechada mantinha callbacks, ações e popovers | Censo mantendo a aplicação viva |
| `3bd601a` | Workers ociosos guardavam o último pedido/payload | Weakrefs liberadas sem enviar outro pedido |
| `89b0d81` | Preferências lembradas forçavam fsync de arquivo e diretório | Três gravações: seis fsync antes, zero depois; JSON íntegro |
| `a3d2ecb` | Provider CSS por janela continuava registrado no display | Finalização do provider no teardown |
| `c38c5f7` | Fila, drag controllers, ícone de arraste e CSS retidos | Censo com preparação e término de arraste |

Os contratos estão em AGENTS.md. Os testes novos são
`tests/test_resource_lifetimes.py` e `tests/test_worker_lifetimes.py`, mais o caso
de gravação em `tests/test_config.py`. A regra geral foi registrada também em
`~/.agents/skills/linux-ui-a11y/references/memory-leak-checking.md`.

## Evidência

Máquina única, GTK 4.22.4, libadwaita 1.9.3, PyGObject 3.56.3, Python 3.14,
renderer Cairo. Interfaces executadas somente em sessão isolada. Abertura dos
diálogos confirmada por AT-SPI durante o sweep; sem percorrer AT-SPI nas janelas
de medição de CPU. Artefatos executáveis e bibliotecas instrumentadas ficaram em
`~/.cache/audio-resource-audit/`; nenhuma biblioteca do sistema foi substituída.

- Antes, cinco instâncias de vários widgets fechados resultavam em zero
  finalizações. Depois, os testes exigem `new == fin` por destrutor de qdata.
- O censo profundo final acompanhou **5.800 widgets criados e 5.800 finalizados**
  em cinco ciclos de MainWindow, com a aplicação ainda viva. Isso cobre a árvore
  de widgets observada, não todos os tipos GObject internos das bibliotecas.
- Suíte final: **187 passed, 7 deselected** (`not extended`). Ruff e formatação
  passaram. O teste de arraste ampliado passou novamente após a suíte completa.
- O sweep cobriu 11 superfícies: boas-vindas, sobre, segmentos, informações,
  resultados, mensagem, limpar, lixeira, marcador, arquivos e pasta. Nove não
  mostraram crescimento fora dos AdwBreakpoint conhecidos; os dois seletores
  ainda mostraram os objetos GTK descritos abaixo. O sweep global não está limpo.

Memória obtida de `/proc/PID/smaps_rollup`, após três ciclos de aquecimento.
Cada valor abaixo é a inclinação de Anonymous entre os ciclos 5 e 25, em KiB/ciclo,
em três execuções separadas. Os valores medem residência anônima, não bytes vivos.

| Jornada | Antes | Depois |
|---|---|---|
| Boas-vindas, revertendo somente sua correção para o A/B | 415,8 / 430,4 / 403,4 | -13,6 / -15,0 / 68,0 |
| Janela adicional, antes de corrigir CSS e fila | 252,8 / 240,0 / 251,8 | 27,2 / 39,0 / 28,4 |
| Controle sem criar a superfície | — | -17,0 / -15,2 / -17,0 |

O processo hospedeiro permaneceu aberto: não se comparou primeira janela a frio
com segunda janela. Os ciclos finais de janela tiveram delta zero de descritores,
sem crescimento de threads e sem swap nas amostras. A máquina usou swap em outras
medições; quedas de memória residente não foram interpretadas como liberação.
Não foi estabelecido um limiar universal de Anonymous: a prova de teardown é o
censo, e os números residuais acima continuam explícitos.

Entrada real com 0, 5 e 25 arquivos WAV, repetida três vezes após a última correção:
Anonymous com 25 arquivos ficou em 46.964–47.016 KiB, cerca de 3,0–3,3 MiB acima
da fila vazia; os 17 descritores permaneceram constantes. A primeira carga criou
dois workers, sem crescimento adicional com 25 arquivos. Remover as linhas não
devolveu imediatamente toda a memória residente, apesar de sua finalização.
Diagnósticos separados com `MALLOC_TRIM_THRESHOLD_=0` reduziram as inclinações de
segmentos/resultados para 9,8/13,2 KiB por ciclo; foram execuções únicas, não uma
estimativa repetida de economia nem uma configuração recomendada ao usuário.

CPU ociosa: três intervalos de 60 s em GLib.MainLoop, depois de aquecimento,
consumiram 0,02 / 0,01 / 0,01 s de CPU, zero `write_bytes` e uma troca voluntária
de contexto por intervalo. Uma versão anterior da sonda fazia polling a cada
5 ms; esses resultados foram descartados. Isso não é uma medição de interrupções
do sistema inteiro. Preferências continuam com substituição atômica; queda de
energia pode perder o último estado lembrado. Durabilidade de áudio não mudou.

Heaptrack: mesma jornada real do editor de segmentos, 5 versus 25 ciclos,
`PYTHONMALLOC=malloc`, Cairo, `GTK_A11Y=none` somente nessa captura. O comando
`heaptrack_print -f heap-final-long.zst -d heap-final-short.zst -l` mostrou saldo
retido de **145,21 KB** e delta de pico de **747,12 KB**. Cairo é o maior grupo
positivo, com 261,05 KB, parcialmente compensado por grupos negativos. Isso não
prova crescimento linear nem permite atribuir todo o saldo a um único defeito.
Capturas que travaram na inicialização foram descartadas; não se misturaram
pares incompletos nem o ensaio reduzido de componente com a aplicação completa.

## Dependências: pendências documentadas, não correções instaladas

Rascunhos e patches sugeridos estão em
`~/relatos-upstream/audio-converter-2026-09-27/`. Nada foi enviado upstream.

- **libadwaita:** três AdwBreakpoint retidos por ciclo em vários diálogos;
  crescimento repetido em duas rodadas, embora os diálogos finalizem. O código
  recebe propriedade dos breakpoints sem destrutor dos elementos do array.
- **GTK:** por abertura/cancelamento de seletor, um GtkGestureLongPress e dois
  GCancellable retidos. O primeiro tem criação sem registro como controller;
  reftrace aponta os cancellables para a região GtkPathBar. A hipótese de limpeza
  incompleta em erro/cancelamento ainda exige validação do patch.
- **gtk4-leak-guard:** registros de reftrace intercalavam linhas entre threads.
  Patch local serializando cada registro foi compilado na cache e produziu trace
  analisável. A inicialização concorrente do logger fica fora desse patch mínimo.

Os patches GTK/libadwaita são propostas: não foram compilados nem comprovados por
sweep com bibliotecas corrigidas. Portanto, não se declara ausência total de
vazamentos. `leak_lint.py` só analisa Rust e não analisou esta aplicação Python;
Clippy, Relm4 e flags de compilação Cargo não se aplicam.

## Reproduzir e interpretar o escopo

Gate de regressão, a partir da raiz do projeto:

```sh
/note/bigdesktop/scripts/headless-gate.sh --accessible python -m pytest -q tests/test_resource_lifetimes.py tests/test_worker_lifetimes.py tests/test_config.py
```

Suíte completa não estendida, usando X11 para o teste existente que usa xdotool:

```sh
mkdir -p ~/.cache/audio-resource-audit/test-tmp
TMPDIR="$HOME/.cache/audio-resource-audit/test-tmp" /note/bigdesktop/scripts/headless-gate.sh --accessible xvfb-run -a env GDK_BACKEND=x11 python -m pytest -q -m 'not extended' tests
```

Evidências locais: `~/.cache/audio-resource-audit/`, especialmente
`full-final-tests.log`, `deep-window-after.log`, `all-after.json`,
`window-final-*.jsonl`, `welcome-before-*.jsonl`, `memory-welcome-*.jsonl`,
`queue-final-*.jsonl`, `idle-clean-*.jsonl`, `heap-leaks.txt` e
`source-sha256.json`. Esses artefatos de máquina não foram adicionados ao Git.

A revisão incluiu fontes GLib, callbacks, workers, subprocessos, descritores,
threads e gravação de preferências. Não houve benchmark de disco realmente frio,
evicção do cache de páginas ou comparação entre renderers. O perfil pareado cobre
o editor de segmentos, não todas as combinações de mídia e duração. O teardown
ainda possui joins limitados de workers e término síncrono do libmpv; os ensaios
não estabelecem um limite de latência de fechamento sob I/O bloqueado. Não se
atribui uma garantia de responsividade nesses cenários aos censos de finalização.
