```markdown
# 🌱 GreenOps Auto-Remediator

[![CI](https://github.com/catitodev/greenops-auto-remediator/actions/workflows/ci.yml/badge.svg)](https://github.com/catitodev/greenops-auto-remediator/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![AWS](https://img.shields.io/badge/AWS-CloudFormation-orange.svg)](https://aws.amazon.com/cloudformation/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![AWS Prompt the Planet 2026](https://img.shields.io/badge/AWS-Prompt%20the%20Planet%202026-FF9900.svg)](https://dorahacks.io/hackathon/awsprompttheplanet)

> **Infraestrutura AWS que se otimiza automaticamente para sustentabilidade e custo**
>
> Submission for [AWS Prompt the Planet Challenge 2026](https://dorahacks.io/hackathon/awsprompttheplanet)

---

## 🎯 O Problema

32% da infraestrutura em cloud é desperdício. Startups criam recursos AWS e esquecem deles:

- Ambientes de dev/teste ficam ligados 24/7
- Instâncias são provisionadas maiores que o necessário
- Volumes EBS ficam órfãos, sem anexação
- IPs elásticos não são liberados
- Não há visibilidade de pegada de carbono

Quando o CFO ou CSO pedem relatórios de sustentabilidade, **não existe nada pronto**.

---

## 💡 A Solução

GreenOps Auto-Remediador é um sistema serverless de três módulos:

| Módulo | Função | Resultado |
|--------|--------|-----------|
| **🔍 Discovery** | Varre a conta AWS e identifica waste | Lista priorizada de otimizações |
| **🔧 Remediation** | Aplica otimizações automaticamente | Recursos otimizados, custo reduzido |
| **📊 Reporting** | Gera dashboards e relatórios | Visibilidade de custo e carbono |

```
┌─────────────┐    ┌──────────────┐    ┌─────────────┐
│  Discovery  │───▶│  Remediation │───▶│  Reporting  │
│  (Scan)     │    │  (Optimize)  │    │  (Dashboard)│
└─────────────┘    └──────────────┘    └─────────────┘
       │                   │                   │
       ▼                   ▼                   ▼
  DynamoDB            DynamoDB            CloudWatch
  (findings)          (rollbacks)         (metrics)
```

---

## 🚀 Quick Start

```bash
# 1. Clone o repositório
git clone https://github.com/catitodev/greenops-auto-remediator.git
cd greenops-auto-remediator

# 2. Configure o ambiente
make setup

# 3. Configure credenciais AWS
aws configure

# 4. Faça o deploy
make deploy

# 5. Execute o Discovery
aws lambda invoke --function-name greenops-discovery response.json

# 6. Acesse o dashboard
# https://console.aws.amazon.com/cloudwatch/home#dashboards:name=GreenOps-Executive-Dashboard
```

---

## 📊 Resultados Esperados

Após 90 dias de uso:

| Métrica | Valor |
|---------|-------|
| Redução de waste | **30%+** |
| Economia mensal | **$500-$5,000** (dependendo do tamanho da conta) |
| Carbono evitado | **0.5-5 MTCO2e** |
| Recursos otimizados | **40-200** |
| ROI do sistema | **> 1000%** |

---

## 🏛️ AWS Well-Architected Framework

| Pilar | Implementação no GreenOps |
|-------|--------------------------|
| **Excelência Operacional** | Automação completa, runbooks gerados automaticamente, health checks |
| **Segurança** | IAM least privilege, tag-based access control, CloudTrail audit, approval gates |
| **Confiabilidade** | Serverless (sem SPOF), retries automáticos, dead letter queues, rollback |
| **Performance Efficiency** | Graviton processors, parallel scanning, caching de recomendações |
| **Otimização de Custos** | Serverless billing, lifecycle policies, ROI tracking, payback calculation |
| **Sustentabilidade** | Customer Carbon Footprint Tool, rightsizing, scheduling, regiões de baixo carbono |

> **Nota:** O pilar **Sustentabilidade** é o único não coberto pelos prompts existentes na [AWS Startups Prompt Library](https://aws.amazon.com/startups/prompt-library). Este projeto preenche essa lacuna.

---

## 🏆 AWS Prompt the Planet Challenge 2026

Este projeto é uma submission para o [AWS Prompt the Planet Challenge](https://dorahacks.io/hackathon/awsprompttheplanet), um hackathon global da AWS em parceria com a DoraHacks.

### O que torna este prompt diferente

- **Único prompt** que integra métricas de carbono (MTCO2e) com otimização de custo
- **Endereça o pilar Sustentabilidade** do Well-Architected Framework — o único não coberto na biblioteca AWS
- **Produção-ready** com segurança, rollback, auditoria, e governança
- **48/49 testes unitários passando** com mocks AWS (moto)

### Prêmios

- **$50,000 em créditos AWS Activate** divididos entre 10 vencedores
- Destaque na **AWS Startups Prompt Library** (visualizada por milhares de desenvolvedores)
- Visibilidade global na comunidade AWS

---

## 📚 Documentação

- [Guia de Deploy](docs/deployment/README.md)
- [Arquitetura](docs/architecture/README.md)
- [Troubleshooting](docs/troubleshooting/README.md)
- [Well-Architected Alignment](docs/architecture/well-architected.md)

---

## 🤝 Contribuição

Veja [CONTRIBUTING.md](CONTRIBUTING.md) para guidelines.

## 📄 Licença

[MIT License](LICENSE)
```

