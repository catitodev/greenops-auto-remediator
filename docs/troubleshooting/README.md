```markdown
# Troubleshooting Guide

Guia de resolução de problemas comuns do GreenOps Auto-Remediador.

---

### Compute Optimizer não mostra recomendações

**Causa:** Conta AWS criada há menos de 14 dias, ou Compute Optimizer não foi habilitado. O serviço precisa de dados históricos de utilização para gerar recomendações.

**Solução:**
1. Verifique se o Compute Optimizer está habilitado
2. Se estiver habilitado, aguarde o período de warm-up (14 dias de dados)
3. O Discovery funcionará sem rightsizing até lá — use métricas CloudWatch como fallback

**Verificação:**
```bash
aws compute-optimizer get-enrollment-status
```

Se retornar `"status": "Active"`, está habilitado. Se `"Inactive"`, habilite via console AWS.

---

### Customer Carbon Footprint Tool mostra zero emissões

**Causa:** Conta nova (dados levam até 3 meses para aparecer), ou permissões de billing insuficientes. O serviço processa dados em batch mensal.

**Solução:**
1. Use estimativa de carbono baseada em cálculo próprio (fator de conversão por $ gasto)
2. Documente no relatório: "Dados oficiais da AWS serão exibidos quando disponíveis"
3. Verifique se a conta tem acesso ao Billing Console

**Verificação:**
```bash
aws ce get-cost-and-usage --time-period Start=2026-01-01,End=2026-03-31 --granularity MONTHLY --metrics BLENDED_COST
```

Se retornar custos, a conta tem atividade. O Carbon Footprint Tool aparecerá em 3 meses.

---

### Lambda atinge timeout durante o Discovery

**Causa:** Conta com muitos recursos em múltiplas regiões. O scan sequencial excede o timeout padrão de 15 minutos.

**Solução:**
1. Aumente a memória da Lambda (mais memória = mais CPU = execução mais rápida):
   - 1024 MB para contas médias
   - 2048 MB para contas grandes
2. Execute scan por região, criando múltiplas invocações paralelas
3. Use Step Functions para orquestrar scans de longa duração

**Verificação:**
```bash
aws lambda get-function --function-name greenops-discovery --query 'Configuration.{Timeout:Timeout,MemorySize:MemorySize}'
```

---

### Permission denied durante o Remediation

**Causa:** IAM role não tem permissão para o recurso específico, ou o recurso não tem a tag `GreenOpsManaged=true`.

**Solução:**
1. Verifique se o recurso tem a tag obrigatória:
   ```bash
   aws ec2 describe-tags --filters "Name=resource-id,Values=i-xxx" "Name=key,Values=GreenOpsManaged"
   ```
2. Verifique se a IAM policy tem a ação necessária com condition `GreenOpsManaged=true`
3. Para recursos em outras contas, verifique trust relationship cross-account

**Verificação:**
```bash
aws iam get-role --role-name greenops-remediation-role-development
```

---

### Email de aprovação não chega

**Causa:** SNS topic não configurado corretamente, email do aprovador não confirmou subscription, ou mensagem caiu na caixa de spam.

**Solução:**
1. Verifique se o email confirmou a subscription do SNS (clicou no link de confirmação)
2. Verifique a caixa de spam/promoções
3. Adicione o SNS topic ARN na tabela `greenops-config` do DynamoDB
4. Use um email alternativo ou Slack webhook como fallback

**Verificação:**
```bash
aws sns list-subscriptions-by-topic --topic-arn arn:aws:sns:us-east-1:123456789012:greenops-approvals-development
```

Procure por `"SubscriptionArn"` com valor que não seja `"PendingConfirmation"`.

---

### Rollback falhou para instância redimensionada

**Causa:** A instância EC2 foi terminada (deletada) após o resize. O rollback só funciona para ações reversíveis — não recupera recursos destruídos.

**Solução:**
1. Documente claramente: "Rollback não recupera recursos terminated ou deletados"
2. Para ações destrutivas (delete), sempre exija aprovação manual e backup prévio
3. Use snapshots automáticos antes de ações destrutivas

**Verificação:**
```bash
aws ec2 describe-instances --instance-ids i-xxx --query 'Reservations[0].Instances[0].State.Name'
```

Se retornar `"terminated"`, o rollback não é possível. Restaure de snapshot se existir.

---

### Cost Explorer com dados de 48 horas atrás

**Causa:** AWS processa dados de custo em batch com delay de 24-48 horas. Isso é comportamento normal do serviço.

**Solução:**
1. Use dados do dia anterior (não do dia atual) nos relatórios
2. Documente a latência nos relatórios: "Dados refletem custos até 48h atrás"
3. Para métricas em tempo real, use CloudWatch billing metrics (aproximadas)

**Verificação:**
```bash
aws ce get-cost-and-usage --time-period Start=$(date -d '3 days ago' +%Y-%m-%d),End=$(date -d 'yesterday' +%Y-%m-%d) --granularity DAILY --metrics BLENDED_COST
```

---

### Dashboard widgets mostram "No data"

**Causa:** Lambda não tem permissão `cloudwatch:PutMetricData`, ou o Discovery ainda não executou pela primeira vez.

**Solução:**
1. Verifique se a role IAM do Discovery tem a permissão `cloudwatch:PutMetricData`
2. Execute o Discovery manualmente uma vez para gerar dados iniciais
3. Configure mensagem fallback no dashboard: "Aguardando primeira execução do Discovery"

**Verificação:**
```bash
aws lambda get-policy --function-name greenops-discovery
aws cloudwatch list-metrics --namespace GreenOps
```

Se `list-metrics` retornar vazio, o Discovery ainda não publicou métricas.
```

