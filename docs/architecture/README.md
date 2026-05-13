# Arquitetura — GreenOps Auto-Remediador

Sistema serverless na AWS que descobre recursos desperdiçados, os otimiza automaticamente com segurança, e gera dashboards de sustentabilidade e custo.

---

## Índice

1. [Visão Geral do Sistema](#1-visão-geral-do-sistema)
2. [Componentes Principais](#2-componentes-principais)
3. [Fluxo de Dados](#3-fluxo-de-dados)
4. [Decisões Arquiteturais](#4-decisões-arquiteturais)
5. [Segurança](#5-segurança)
6. [Escalabilidade](#6-escalabilidade)

---

## 1. Visão Geral do Sistema

O GreenOps Auto-Remediador opera em três fases sequenciais, cada uma implementada como uma função Lambda independente:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        GreenOps Auto-Remediador                             │
│                                                                             │
│  ┌──────────────┐    DynamoDB     ┌──────────────┐    DynamoDB             │
│  │  DISCOVERY   │   Stream ──────▶│ REMEDIATION  │   Update                │
│  │              │                 │              │──────────────┐           │
│  │ Escaneia     │   Findings      │ Executa      │              ▼           │
│  │ recursos AWS │──────────────▶  │ ações com    │        ┌──────────┐     │
│  │ a cada 6h    │   (DynamoDB)    │ aprovação    │        │ DynamoDB │     │
│  └──────────────┘                 └──────────────┘        │ Findings │     │
│         │                                │                └──────────┘     │
│         │ EventBridge                    │ SNS                  │           │
│         │ Schedule                       │ Approval             │           │
│         ▼                                ▼                      │           │
│  ┌──────────────┐              ┌──────────────────┐            │           │
│  │  EventBridge │              │  Aprovador       │            │           │
│  │  (6h/24h/8h) │              │  (email/Slack)   │            │           │
│  └──────────────┘              └──────────────────┘            │           │
│                                                                 │           │
│  ┌──────────────────────────────────────────────────────────────┘           │
│  │                                                                          │
│  ▼                                                                          │
│  ┌──────────────┐    CloudWatch   ┌──────────────┐    S3                   │
│  │  REPORTING   │──────────────▶  │  Dashboard   │    Reports              │
│  │              │                 │  Executivo   │──────────────▶ S3       │
│  │ Gera métricas│   SNS Email     └──────────────┘                         │
│  │ semanalmente │──────────────▶  Stakeholders                             │
│  └──────────────┘                                                           │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Recursos escaneados pelo Discovery

| Recurso AWS | Critério de Desperdício | Tipo | Ação Recomendada |
|---|---|---|---|
| EC2 Instance | CPU < 5% por 7 dias | IDLE | STOP |
| EC2 Instance | Compute Optimizer: over-provisioned | OVERSIZED | RESIZE |
| EBS Volume | Desanexado há 30+ dias | ORPHAN | DELETE |
| Elastic IP | Sem associação | ORPHAN | RELEASE |
| RDS Instance | Conexões < 1 por 7 dias | IDLE | STOP |
| Lambda Function | Memória alocada > 3× usada | OVERSIZED | RESIZE |
| S3 Bucket | Sem lifecycle policy | MISCONFIGURED | APPLY_LIFECYCLE |
| Load Balancer | 0 healthy hosts por 7 dias | IDLE | DELETE |

---

## 2. Componentes Principais

### Lambda Functions

| Função | Trigger | Responsabilidade | Timeout | Memória |
|---|---|---|---|---|
| `greenops-discovery` | EventBridge (a cada 6h) | Escaneia recursos AWS e persiste findings | 300s | 256MB |
| `greenops-remediation` | DynamoDB Stream | Processa findings aprovados e executa ações | 300s | 256MB |
| `greenops-reporting` | EventBridge (semanal, seg 08h UTC) | Gera dashboard, email executivo e relatório S3 | 300s | 512MB |

Todas as funções usam:
- **Runtime:** Python 3.12 em arquitetura ARM64 (Graviton2) — ~20% mais barato que x86
- **Lambda Layer:** `greenops-shared` com utilitários comuns (`constants`, `config`, `utils`)
- **Concorrência reservada:** limita blast radius em caso de bug

### DynamoDB Tables

| Tabela | Chave Primária | Índices | TTL | Propósito |
|---|---|---|---|---|
| `greenops-findings` | `resourceId` (PK) + `findingId` (SK) | `ResourceTypeIndex` | 90 dias | Findings descobertos pelo Discovery |
| `greenops-rollbacks` | `remediationId` (PK) + `resourceId` (SK) | `ResourceIdIndex` | 7 dias | Estado anterior para rollback manual |
| `greenops-approvals` | `approvalId` (PK) | `StatusIndex` | 48 horas | Pedidos de aprovação pendentes |
| `greenops-config` | `configKey` (PK) | — | — | Configuração e rate limiting counters |

Todas as tabelas têm:
- **Billing mode:** PAY_PER_REQUEST (sem capacidade provisionada)
- **Encryption:** SSE com KMS
- **Point-in-time recovery:** habilitado

A tabela `greenops-findings` tem **DynamoDB Streams** habilitado com `NEW_AND_OLD_IMAGES`, que aciona a função de Remediation automaticamente quando um finding é aprovado.

### S3 Bucket

- **Nome:** `greenops-reports-{env}-{account_id}`
- **Lifecycle policy:** objetos em `reports/` transitam para Glacier após 90 dias
- **Criptografia:** SSE-KMS com chave dedicada
- **Acesso público:** completamente bloqueado
- **Versionamento:** habilitado

### SNS Topics

| Tópico | Propósito | Assinantes |
|---|---|---|
| `greenops-notifications` | Relatórios semanais e confirmações de remediação | Email do time de ops |
| `greenops-approvals` | Pedidos de aprovação para ações MEDIUM/HIGH/CRITICAL | Email do aprovador |
| `greenops-alerts` | Erros críticos e falhas de Lambda | Email do time de ops |

### CloudWatch

- **Log Groups:** `/aws/lambda/greenops-{discovery,remediation,reporting}` com retenção de 30 dias
- **Alarms:** erros de Lambda, throttling, throttles de DynamoDB, custo estimado alto
- **Dashboard:** `GreenOps-Executive-Dashboard` com 8 widgets (savings, carbono, taxa de otimização, tendência de custo, top findings)
- **Namespaces customizados:**
  - `GreenOps/Discovery` — findings por tipo, savings potenciais
  - `GreenOps/Remediation` — remediações aplicadas, falhas, custo economizado
  - `GreenOps/Reporting` — métricas agregadas semanais

### EventBridge Rules

| Regra | Schedule | Target | Estado em Prod |
|---|---|---|---|
| `greenops-discovery-schedule` | `rate(6 hours)` | Lambda Discovery | ENABLED |
| `greenops-remediation-schedule` | `rate(24 hours)` | Lambda Remediation | **DISABLED** (usa Stream) |
| `greenops-reporting-schedule` | `cron(0 8 * * ? *)` | Lambda Reporting | ENABLED |

---

## 3. Fluxo de Dados

### Fase 1 — Discovery (a cada 6 horas)

```
EventBridge Schedule
        │
        ▼
Lambda Discovery
        │
        ├─── ec2.describe_instances (tag: GreenOpsManaged=true)
        ├─── cloudwatch.get_metric_statistics (CPUUtilization 7 dias)
        ├─── compute_optimizer.get_ec2_instance_recommendations
        ├─── ec2.describe_volumes (status: available)
        ├─── ec2.describe_addresses
        ├─── rds.describe_db_instances + cloudwatch (DatabaseConnections)
        ├─── lambda.list_functions + cloudwatch (MemorySize)
        ├─── s3.list_buckets + s3.get_bucket_lifecycle_configuration
        └─── elbv2.describe_load_balancers + cloudwatch (HealthyHostCount)
                │
                ▼
        Findings calculados com:
        - wasteType, severity, description
        - PriorityScore = f(savings, carbon, severity, confidence)
        - TTL = now + 90 dias
                │
                ▼
        DynamoDB batch_writer → greenops-findings
                │
                ▼
        CloudWatch PutMetricData → GreenOps/Discovery
```

### Fase 2 — Remediation (via DynamoDB Stream)

```
DynamoDB Stream (INSERT/MODIFY onde status=APPROVED)
        │
        ▼
Lambda Remediation
        │
        ├─── classify_risk(finding)
        │       ├── tag Environment=Production → CRITICAL
        │       └── ambiente prod → eleva um nível
        │
        ├─── [CRITICAL/HIGH/MEDIUM] → request_approval()
        │       ├── DynamoDB PUT → greenops-approvals
        │       └── SNS Publish → greenops-approvals topic
        │
        └─── [LOW] → check_rate_limit()
                │       ├── DynamoDB UpdateItem ADD → rate:total:{hora}
                │       ├── DynamoDB UpdateItem ADD → rate:type:{action}:{hora}
                │       └── DynamoDB UpdateItem ADD → rate:destructive:{dia}
                │
                ▼
        execute_action(finding)
                │
                ├── save_rollback_state() → greenops-rollbacks (TTL 7 dias)
                │
                ├── [EC2]    ec2.stop_instances / start_instances / modify_instance_attribute
                ├── [EBS]    ec2.create_snapshot + ec2.delete_volume
                ├── [EIP]    ec2.release_address
                ├── [RDS]    rds.stop_db_instance / start_db_instance
                ├── [Lambda] lambda.update_function_configuration
                ├── [S3]     s3.put_bucket_lifecycle_configuration
                └── [ALB]    elbv2.delete_load_balancer
                        │
                        ▼
                DynamoDB UpdateItem → finding.status = REMEDIATED
                CloudWatch PutMetricData → GreenOps/Remediation
```

### Fase 3 — Reporting (semanal)

```
EventBridge Schedule (toda segunda, 08:00 UTC)
        │
        ▼
Lambda Reporting
        │
        ├─── DynamoDB Scan → greenops-findings (todos os findings)
        │
        ├─── Calcula métricas:
        │       ├── total_savings (soma REMEDIATED)
        │       ├── total_carbon_reduction
        │       ├── optimization_rate = remediated/total * 100
        │       ├── trees_equivalent = carbon * 45
        │       └── cars_equivalent = carbon / 4.6
        │
        ├─── Cost Explorer → custo mês atual e anterior
        │       └── cost_trend = (atual - anterior) / anterior * 100
        │
        ├─── CloudWatch PutDashboard → GreenOps-Executive-Dashboard
        │       └── 8 widgets: savings, carbono, taxa, tendência, waste%, top10, serviços
        │
        ├─── CloudWatch PutMetricData → GreenOps/Reporting
        │
        ├─── SNS Publish → greenops-notifications
        │       └── Email executivo: KPIs + top 3 ações + alertas
        │
        └─── S3 PutObject → reports/YYYY/MM/DD/report.json
```

### Formato do Finding

Cada finding é um documento JSON com a seguinte estrutura:

```json
{
  "findingId": "uuid-v5-determinístico",
  "timestamp": "2026-05-13T08:00:00+00:00",
  "resourceType": "AWS::EC2::Instance",
  "resourceId": "i-1234567890abcdef0",
  "region": "us-east-1",
  "wasteType": "IDLE",
  "severity": "HIGH",
  "description": "Instância EC2 i-xxx com CPU médio de 2.3% nos últimos 7 dias",
  "metrics": {
    "avgCpuUtilizationPct": 2.3,
    "observationDays": 7,
    "instanceType": "t3.medium"
  },
  "recommendation": {
    "action": "STOP",
    "reason": "CPU consistentemente abaixo de 5% por 7 dias",
    "estimatedMonthlySavings": 30.0,
    "estimatedMonthlyCarbonReduction": 0.05,
    "riskLevel": "MEDIUM"
  },
  "tags": { "GreenOpsManaged": "true", "Environment": "dev" },
  "priorityScore": 27.98,
  "status": "APPROVED",
  "ttl": 1760000000
}
```

**PriorityScore** é calculado pela fórmula:

```
PriorityScore = (
    min(savings / 500, 1.0) × 0.40 +
    min(carbon / 5.0,  1.0) × 0.30 +
    (severity_weight / 100) × 0.20 +
    confidence              × 0.10
) × 100
```

---

## 4. Decisões Arquiteturais

### Por que Serverless (Lambda)?

| Critério | Serverless | Alternativa (ECS/EC2) |
|---|---|---|
| **Custo** | Paga apenas por execução (~6h/dia) | Instância rodando 24/7 |
| **Operação** | Zero servidores para gerenciar | Patching, scaling, monitoring |
| **Escala** | Automática por invocação | Requer Auto Scaling Group |
| **Isolamento** | Cada scan em execução separada | Processo compartilhado |
| **Cold start** | ~500ms (aceitável para scans periódicos) | Não aplicável |

O sistema roda scans a cada 6 horas — não há justificativa para manter infraestrutura ativa 24/7. Lambda elimina ~95% do custo de compute comparado a uma instância t3.medium dedicada.

### Por que DynamoDB?

| Critério | DynamoDB | Alternativa (RDS/Aurora) |
|---|---|---|
| **Schema** | Flexível — findings têm estruturas variadas por tipo | Schema rígido requer migrações |
| **Escala** | PAY_PER_REQUEST — zero capacity planning | Requer sizing de instância |
| **Streams** | Nativo — aciona Remediation automaticamente | Requer polling ou CDC externo |
| **TTL** | Nativo — findings expiram automaticamente | Requer job de limpeza |
| **Operação** | Serverless — sem patches ou backups manuais | Requer manutenção de instância |

O DynamoDB Stream é o mecanismo central de integração entre Discovery e Remediation — elimina polling e garante processamento event-driven com at-least-once delivery.

### Por que CloudFormation?

| Critério | CloudFormation | Alternativa (CDK/Terraform) |
|---|---|---|
| **Dependências** | Zero — nativo AWS, sem instalação | CDK requer Node.js; Terraform requer binário |
| **Drift detection** | Nativo no console AWS | Requer ferramentas externas |
| **IAM** | `CAPABILITY_NAMED_IAM` nativo | Mesma capacidade |
| **Rollback** | Automático em falha de deploy | Requer configuração |
| **Curva de aprendizado** | YAML declarativo — familiar para ops | CDK requer conhecimento de linguagem |

Para um sistema de FinOps/GreenOps que será operado por times de ops (não apenas devs), CloudFormation YAML é mais acessível e auditável.

### Por que ARM64 (Graviton2)?

Lambda em ARM64 oferece ~20% de redução de custo e ~19% de melhoria de performance para workloads Python comparado a x86_64, sem nenhuma mudança no código Python. O GreenOps pratica o que prega.

### Fluxo de aprovação por nível de risco

```
RiskLevel   │ Fluxo                                    │ Exemplos
────────────┼──────────────────────────────────────────┼──────────────────────
LOW         │ Auto-execute imediatamente               │ TAG, APPLY_LIFECYCLE
MEDIUM      │ SNS → aprovação simples → execute        │ STOP de EC2 idle
HIGH        │ SNS → dry-run → aprovação → execute      │ RESIZE de EC2/RDS
CRITICAL    │ SNS → aprovação dupla → execute          │ DELETE de volume EBS
```

Recursos com tag `Environment=Production` sempre recebem `CRITICAL`, independente do risco declarado.

---

## 5. Segurança

### IAM — Least Privilege

Cada função Lambda tem uma IAM Role dedicada com permissões mínimas:

| Role | Permissões de Escrita | Permissões de Leitura |
|---|---|---|
| `greenops-discovery-role` | DynamoDB (findings, config), SNS publish | EC2, RDS, Lambda, CloudWatch, Compute Optimizer, Cost Explorer |
| `greenops-remediation-role` | EC2 (stop/start/modify), RDS (stop/start), Lambda (update config), S3 (lifecycle), DynamoDB (findings, rollbacks, approvals), SNS | EC2, RDS, Lambda describe |
| `greenops-reporting-role` | CloudWatch (dashboards, metrics), S3 (reports), SNS publish | DynamoDB (todas as tabelas), Cost Explorer, Carbon Footprint |

### Tag-Based Access Control

As IAM policies de remediação incluem condições baseadas em tags que impedem ações em recursos não autorizados:

```yaml
Condition:
  StringEquals:
    aws:ResourceTag/GreenOpsManaged: "true"
  StringNotEquals:
    aws:ResourceTag/Environment: "Production"
```

Isso significa que mesmo que um bug no código tente remediar um recurso não autorizado, a IAM policy bloqueia a chamada na camada de controle de acesso — defesa em profundidade.

### Criptografia

| Recurso | Criptografia em Repouso | Criptografia em Trânsito |
|---|---|---|
| DynamoDB | SSE com KMS (chave gerenciada pela AWS) | TLS 1.2+ (HTTPS obrigatório) |
| S3 | SSE-KMS com chave dedicada por ambiente | TLS 1.2+ (bucket policy nega HTTP) |
| SNS | SSE com `alias/aws/sns` | TLS 1.2+ |
| Lambda env vars | Criptografadas em repouso pelo KMS | N/A |
| CloudWatch Logs | Criptografia padrão AWS | TLS 1.2+ |

### Proteção contra ações acidentais

1. **Dry-run mode:** `GREENOPS_DRY_RUN=true` simula todas as ações sem executar nada. Padrão em todos os ambientes não-prod.
2. **Tag de proteção:** recursos com `GreenOpsProtected=true` são ignorados em todos os scans.
3. **Rate limiting:** máx 50 ações/hora total, 20/hora por tipo, 10 destrutivas/dia — contadores atômicos no DynamoDB.
4. **Rollback state:** estado anterior salvo no DynamoDB antes de qualquer ação, com TTL de 7 dias.
5. **Aprovação dupla:** ações CRITICAL requerem dois aprovadores independentes.

### Auditoria

- **CloudTrail:** todas as chamadas de API AWS são registradas automaticamente
- **CloudWatch Logs:** cada ação de remediação é logada com `remediationId`, `resourceId`, `action`, `previousState`
- **DynamoDB rollbacks table:** snapshot do estado anterior de cada recurso modificado

---

## 6. Escalabilidade

### Limites por design

O sistema foi projetado para operar em contas AWS com centenas a milhares de recursos:

| Componente | Limite atual | Estratégia de escala |
|---|---|---|
| Discovery scan | ~1.000 recursos/execução | Paginação nativa em todas as APIs AWS |
| DynamoDB batch_writer | 25 itens/batch (AWS limit) | Automático via SDK |
| Compute Optimizer | 100 ARNs por chamada | Chunking implementado no código |
| CloudWatch metrics | 20 métricas por PutMetricData | Chunking implementado no código |
| Lambda concorrência | 5 (discovery), 3 (remediation), 2 (reporting) | Ajustável via parâmetro CloudFormation |
| Rate limiting | 50 ações/hora, 10 destrutivas/dia | Configurável via env vars |

### Contas com muitos recursos (1.000+)

Para contas com volume alto de recursos, recomenda-se:

1. **Aumentar o timeout da Lambda** — o parâmetro `LambdaTimeout` no CloudFormation aceita até 900s
2. **Aumentar a memória** — `LambdaMemorySize` de 256MB para 512MB ou 1024MB melhora CPU e throughput
3. **Paralelizar por região** — deploy do stack em múltiplas regiões com stacks independentes
4. **Filtrar por tags adicionais** — adicionar tags como `Team` ou `CostCenter` ao `REQUIRED_TAGS` para escanear subconjuntos

### Multi-conta (AWS Organizations)

Para organizações com múltiplas contas AWS:

```
┌─────────────────────────────────────────────────────┐
│                  Management Account                  │
│                                                      │
│  GreenOps Reporting (agregado)                       │
│  ├── Cost Explorer (multi-account)                   │
│  └── CloudWatch cross-account dashboard              │
└─────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  Account A   │   │  Account B   │   │  Account C   │
│  GreenOps    │   │  GreenOps    │   │  GreenOps    │
│  Discovery + │   │  Discovery + │   │  Discovery + │
│  Remediation │   │  Remediation │   │  Remediation │
└──────────────┘   └──────────────┘   └──────────────┘
```

Cada conta tem seu próprio stack CloudFormation. O reporting pode ser centralizado na conta de management usando IAM roles cross-account.

### Observabilidade em escala

O sistema publica métricas customizadas no CloudWatch com dimensão `Environment`, permitindo:

- Filtrar por ambiente (dev/staging/prod) no mesmo dashboard
- Criar alarmes por ambiente com thresholds diferentes
- Agregar métricas de múltiplas regiões via CloudWatch cross-region

---

## Referências

- [AWS Lambda Best Practices](https://docs.aws.amazon.com/lambda/latest/dg/best-practices.html)
- [DynamoDB Best Practices](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/best-practices.html)
- [AWS Well-Architected Framework — Sustainability Pillar](https://docs.aws.amazon.com/wellarchitected/latest/sustainability-pillar/sustainability-pillar.html)
- [EPA — Greenhouse Gas Equivalencies Calculator](https://www.epa.gov/energy/greenhouse-gas-equivalencies-calculator)
- [AWS Compute Optimizer](https://docs.aws.amazon.com/compute-optimizer/latest/ug/what-is-compute-optimizer.html)
