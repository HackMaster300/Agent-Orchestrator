# Contribuir para o Agent-Orchestrator

O Agent-Orchestrator é um produto da **Zharak** e aceita contribuições da comunidade.

## Como ajudar
- **Validar em sessões reais:** o protocolo está em [`verify-assumptions.md`](skills/agent-orchestrator/references/verify-assumptions.md). Relatos do que funcionou ou falhou valem muito.
- **Scripts:** melhorias em `orch.py` e no hook `writeset_guard.py`.
- **Instruções da skill:** `SKILL.md`, `plan-schema.md` e `brief-templates.md` (em inglês, como o resto das instruções para o Claude).
- **Diagramas e documentação:** correcções e novos diagramas em `docs/img/`.
- **Issues:** colisões, estados perdidos, retentativas mal comportadas, gastos de tokens inesperados.

## Antes de abrir um pull request
1. Abre uma issue para mudanças grandes.
2. Corre o autoteste e garante que passa:
   ```bash
   python3 skills/agent-orchestrator/scripts/selftest.py
   ```
3. Se mudares comportamento, acrescenta uma verificação ao `selftest.py`.
4. Um pull request, um assunto. Explica o porquê, não só o quê.
5. Mantém as funções simples: complexidade ciclomática de **11 ou menos** por função.
6. Diagramas SVG: mantém o rodapé "Developed by Zharak" e testa a animação (`<g id="motion">`) num browser.
7. Não incluas segredos, dados pessoais ou código de clientes em issues e PRs.

## Licença das contribuições
Ao contribuir, aceitas que o teu trabalho é licenciado sob a [Apache-2.0](LICENSE), como o resto do projecto.

## Marca
O nome e o logótipo Zharak não estão licenciados (ver [NOTICE](NOTICE)). Forks podem usar o código, mas não devem sugerir que são feitos ou aprovados pela Zharak.
