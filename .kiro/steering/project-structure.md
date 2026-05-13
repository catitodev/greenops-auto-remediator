# Estrutura do Projeto

## src/
Codigo-fonte dos modulos Lambda
- discovery/ — varredura de recursos
- remediation/ — aplicacao de otimizacoes
- reporting/ — geracao de dashboards e relatorios
- shared/ — utilitarios comuns

## infrastructure/
Templates de infraestrutura
- cloudformation/main.yaml — template principal

## docs/
Documentacao do projeto
- architecture/ — decisoes arquiteturais
- deployment/ — guia de deploy
- troubleshooting/ — problemas comuns

## tests/
Testes automatizados
- unit/ — testes unitarios com moto
- integration/ — testes de integracao
- e2e/ — testes end-to-end

## scripts/
Automacao de comandos
- setup.sh — configuracao inicial
- deploy.sh — deploy na AWS
- test.sh — execucao de testes

