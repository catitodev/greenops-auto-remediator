# Troubleshooting Guide

Guia de resolução de problemas comuns do GreenOps Auto-Remediador.

---

### Compute Optimizer não mostra recomendações

**Causa:** Conta AWS criada há menos de 14 dias, ou Compute Optimizer não foi habilitado. O serviço precisa de dados históricos de utilização para gerar recomendações.

**Solução:**
1. Verifique se o Compute Optimizer está habilitado:
2. Se estiver habilitado, aguarde o período de warm-up (14 dias de dados)
3. O Discovery funcionará sem rightsizing até lá — use métricas CloudWatch como fallback

**Verificação:**
```bash
aws compute-optimizer get-enrollment-status