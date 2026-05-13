"""
GreenOps Auto-Remediador — Reporting Handler
============================================
Gera relatórios executivos de sustentabilidade e custo, atualiza o
CloudWatch Dashboard e envia email semanal via SNS.

Entry point Lambda: ``lambda_handler(event, context)``
Trigger: EventBridge schedule (semanal, toda segunda às 08:00 UTC)

Responsabilidades:
1. Ler todos os findings do DynamoDB e calcular métricas agregadas
2. Buscar custos reais do mês atual e anterior via Cost Explorer
3. Gerar/atualizar CloudWatch Dashboard "GreenOps-Executive-Dashboard"
4. Publicar métricas customizadas no namespace GreenOps/Reporting
5. Enviar email executivo via SNS com resumo de savings e carbono
6. Salvar relatório JSON completo no S3

Variáveis de ambiente esperadas:
- FINDINGS_TABLE: tabela DynamoDB de findings
- NOTIFICATIONS_TOPIC: ARN do tópico SNS para email executivo
- REPORTS_BUCKET: bucket S3 para armazenar relatórios JSON
- AWS_REGION: região AWS (padrão: us-east-1)
- ENVIRONMENT: ambiente de execução (padrão: dev)
"""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from shared.utils import (
    estimate_cars_equivalent,
    estimate_trees_equivalent,
    format_carbon,
    format_currency,
)

# =============================================================================
# Constantes
# =============================================================================

# Nome fixo do dashboard executivo no CloudWatch
DASHBOARD_NAME = "GreenOps-Executive-Dashboard"

# Número de findings pendentes exibidos no widget de tabela
TOP_PENDING_FINDINGS = 10

# Número de ações destacadas no email executivo
TOP_ACTIONS_EMAIL = 3


# =============================================================================
# Helpers internos
# =============================================================================

def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso_now() -> str:
    return _now_utc().isoformat()


def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Converte Decimal/str/int para float de forma segura."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _month_boundaries(offset_months: int = 0) -> tuple[str, str]:
    """
    Retorna (start, end) no formato YYYY-MM-DD para o mês atual ou anterior.

    Args:
        offset_months: 0 = mês atual, -1 = mês anterior.

    Returns:
        tuple[str, str]: (primeiro_dia, ultimo_dia) do mês.
    """
    now = _now_utc()
    # Primeiro dia do mês atual
    first_current = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if offset_months == 0:
        start = first_current
        end = now
    else:
        # Mês anterior: vai para o último dia do mês anterior
        last_prev = first_current - timedelta(days=1)
        start = last_prev.replace(day=1)
        end = last_prev

    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# =============================================================================
# ReportingGenerator
# =============================================================================

class ReportingGenerator:
    """
    Gera relatórios executivos de FinOps e GreenOps.

    Args:
        region: Região AWS. Padrão: AWS_REGION env var ou us-east-1.
        session: Sessão boto3 customizada (útil para testes com moto).
    """

    def __init__(
        self,
        region: str | None = None,
        session: boto3.Session | None = None,
    ) -> None:
        self.region = region or _get_env("AWS_REGION", "us-east-1")
        self._session = session or boto3.Session(region_name=self.region)

        self.findings_table_name = _get_env("FINDINGS_TABLE")
        self.notifications_topic = _get_env("NOTIFICATIONS_TOPIC")
        self.reports_bucket = _get_env("REPORTS_BUCKET")
        self.environment = _get_env("ENVIRONMENT", "dev")

        # Lazy clients
        self._dynamodb: Any = None
        self._cw: Any = None
        self._sns: Any = None
        self._s3: Any = None
        self._ce: Any = None  # Cost Explorer (somente us-east-1)

    # -------------------------------------------------------------------------
    # Lazy clients
    # -------------------------------------------------------------------------

    @property
    def dynamodb(self) -> Any:
        if self._dynamodb is None:
            self._dynamodb = self._session.resource("dynamodb")
        return self._dynamodb

    @property
    def cw(self) -> Any:
        if self._cw is None:
            self._cw = self._session.client("cloudwatch", region_name=self.region)
        return self._cw

    @property
    def sns(self) -> Any:
        if self._sns is None:
            self._sns = self._session.client("sns", region_name=self.region)
        return self._sns

    @property
    def s3(self) -> Any:
        if self._s3 is None:
            self._s3 = self._session.client("s3", region_name=self.region)
        return self._s3

    @property
    def ce(self) -> Any:
        """Cost Explorer só está disponível em us-east-1."""
        if self._ce is None:
            self._ce = self._session.client("ce", region_name="us-east-1")
        return self._ce

    # -------------------------------------------------------------------------
    # 1. Leitura de findings do DynamoDB
    # -------------------------------------------------------------------------

    def fetch_all_findings(self) -> list[dict[str, Any]]:
        """
        Lê todos os findings da tabela DynamoDB via scan paginado.

        Usa ProjectionExpression para buscar apenas os campos necessários
        para o relatório, reduzindo consumo de RCU.

        Returns:
            Lista de findings como dicts Python. Lista vazia se a tabela
            não estiver configurada ou ocorrer erro.
        """
        if not self.findings_table_name:
            print("[WARN] fetch_all_findings: FINDINGS_TABLE não configurada")
            return []

        findings: list[dict[str, Any]] = []

        try:
            table = self.dynamodb.Table(self.findings_table_name)
            paginator_kwargs: dict[str, Any] = {
                # Campos necessários para o relatório
                "ProjectionExpression": (
                    "findingId, resourceId, resourceType, #st, wasteType, "
                    "severity, priorityScore, recommendation, #ts, #reg"
                ),
                "ExpressionAttributeNames": {
                    "#st":  "status",
                    "#ts":  "timestamp",
                    "#reg": "region",
                },
            }

            # Scan paginado
            response = table.scan(**paginator_kwargs)
            findings.extend(response.get("Items", []))

            while "LastEvaluatedKey" in response:
                paginator_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
                response = table.scan(**paginator_kwargs)
                findings.extend(response.get("Items", []))

            print(f"[INFO] fetch_all_findings: {len(findings)} findings lidos")

        except ClientError as exc:
            print(f"[ERROR] fetch_all_findings: erro DynamoDB: {exc}")

        return findings

    # -------------------------------------------------------------------------
    # 2. Cálculo de métricas agregadas
    # -------------------------------------------------------------------------

    def calculate_metrics(
        self, findings: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        Calcula métricas agregadas a partir da lista de findings.

        Métricas calculadas:
        - total_findings, remediated_count, pending_count, failed_count
        - optimization_rate (remediated / total * 100)
        - total_savings (soma de estimatedMonthlySavings dos REMEDIATED)
        - total_carbon_reduction (soma de estimatedMonthlyCarbonReduction dos REMEDIATED)
        - potential_savings (soma de savings de todos os findings não-REMEDIATED)
        - trees_equivalent, cars_equivalent (via utils.py)
        - by_waste_type, by_severity, by_resource_type (contagens)
        - top_pending (top 10 por priorityScore)
        - cost_by_service (savings agrupados por resourceType)

        Args:
            findings: Lista de findings do DynamoDB.

        Returns:
            dict com todas as métricas calculadas.
        """
        total = len(findings)
        remediated_count = 0
        pending_count = 0
        failed_count = 0
        rate_limited_count = 0

        total_savings = 0.0
        total_carbon = 0.0
        potential_savings = 0.0

        by_waste: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        by_resource_type: dict[str, float] = {}  # resourceType → savings

        pending_findings: list[dict[str, Any]] = []

        for f in findings:
            status = f.get("status", "UNKNOWN")
            rec = f.get("recommendation", {})
            savings = _safe_float(rec.get("estimatedMonthlySavings", 0))
            carbon = _safe_float(rec.get("estimatedMonthlyCarbonReduction", 0))
            waste = f.get("wasteType", "UNKNOWN")
            severity = f.get("severity", "UNKNOWN")
            resource_type = f.get("resourceType", "UNKNOWN").split("::")[-1]  # ex: "Instance"

            # Contagens por status
            if status == "REMEDIATED":
                remediated_count += 1
                total_savings += savings
                total_carbon += carbon
            elif status in ("APPROVED", "PENDING", "PENDING_APPROVAL"):
                pending_count += 1
                potential_savings += savings
                pending_findings.append(f)
            elif status == "FAILED":
                failed_count += 1
            elif status == "RATE_LIMITED":
                rate_limited_count += 1
                pending_count += 1
                potential_savings += savings
                pending_findings.append(f)

            # Agrupamentos
            by_waste[waste] = by_waste.get(waste, 0) + 1
            by_severity[severity] = by_severity.get(severity, 0) + 1
            by_resource_type[resource_type] = (
                by_resource_type.get(resource_type, 0.0) + savings
            )

        # Taxa de otimização
        optimization_rate = round((remediated_count / total * 100) if total > 0 else 0.0, 1)

        # Top 10 findings pendentes por priorityScore (desc)
        top_pending = sorted(
            pending_findings,
            key=lambda x: _safe_float(x.get("priorityScore", 0)),
            reverse=True,
        )[:TOP_PENDING_FINDINGS]

        # Equivalências de carbono
        trees = estimate_trees_equivalent(total_carbon)
        cars = estimate_cars_equivalent(total_carbon)

        # Waste percentage por tipo (para gráfico de barras)
        waste_pct: dict[str, float] = {}
        if total > 0:
            waste_pct = {k: round(v / total * 100, 1) for k, v in by_waste.items()}

        return {
            "total_findings": total,
            "remediated_count": remediated_count,
            "pending_count": pending_count,
            "failed_count": failed_count,
            "rate_limited_count": rate_limited_count,
            "optimization_rate": optimization_rate,
            "total_savings": round(total_savings, 2),
            "total_carbon_reduction": round(total_carbon, 4),
            "potential_savings": round(potential_savings, 2),
            "trees_equivalent": trees,
            "cars_equivalent": cars,
            "by_waste_type": by_waste,
            "by_severity": by_severity,
            "by_resource_type": {k: round(v, 2) for k, v in by_resource_type.items()},
            "waste_percentage": waste_pct,
            "top_pending_findings": top_pending,
        }

    # -------------------------------------------------------------------------
    # 3. Cost Explorer — custos reais
    # -------------------------------------------------------------------------

    def fetch_cost_data(self) -> dict[str, float]:
        """
        Busca custos reais do mês atual e anterior via Cost Explorer API.

        Retorna custo total da conta AWS (não apenas recursos GreenOps),
        usado para calcular o cost_trend e contextualizar as economias.

        Returns:
            dict com:
            - current_month_cost: custo acumulado do mês atual (USD)
            - previous_month_cost: custo total do mês anterior (USD)
            - cost_trend: variação percentual (positivo = aumento, negativo = redução)
        """
        result = {
            "current_month_cost": 0.0,
            "previous_month_cost": 0.0,
            "cost_trend": 0.0,
        }

        try:
            # Mês atual
            curr_start, curr_end = _month_boundaries(0)
            curr_resp = self.ce.get_cost_and_usage(
                TimePeriod={"Start": curr_start, "End": curr_end},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
            )
            curr_results = curr_resp.get("ResultsByTime", [])
            if curr_results:
                curr_amount = curr_results[0].get("Total", {}).get("UnblendedCost", {}).get("Amount", "0")
                result["current_month_cost"] = round(float(curr_amount), 2)

            # Mês anterior
            prev_start, prev_end = _month_boundaries(-1)
            prev_resp = self.ce.get_cost_and_usage(
                TimePeriod={"Start": prev_start, "End": prev_end},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
            )
            prev_results = prev_resp.get("ResultsByTime", [])
            if prev_results:
                prev_amount = prev_results[0].get("Total", {}).get("UnblendedCost", {}).get("Amount", "0")
                result["previous_month_cost"] = round(float(prev_amount), 2)

            # Tendência percentual
            prev = result["previous_month_cost"]
            curr = result["current_month_cost"]
            if prev > 0:
                result["cost_trend"] = round((curr - prev) / prev * 100, 1)

            print(
                f"[INFO] fetch_cost_data: atual=${result['current_month_cost']:.2f}, "
                f"anterior=${result['previous_month_cost']:.2f}, "
                f"trend={result['cost_trend']:+.1f}%"
            )

        except ClientError as exc:
            # Cost Explorer pode não estar habilitado em todas as contas
            print(f"[WARN] fetch_cost_data: Cost Explorer indisponível: {exc}")

        return result

    # -------------------------------------------------------------------------
    # 4. CloudWatch Dashboard
    # -------------------------------------------------------------------------

    def build_dashboard_body(
        self,
        metrics: dict[str, Any],
        cost_data: dict[str, float],
    ) -> str:
        """
        Constrói o JSON do CloudWatch Dashboard com 8 widgets.

        Widgets:
        1. Número: Total Savings (REMEDIATED)
        2. Número: Carbon Avoided (MTCO2e)
        3. Número: Resources Optimized (count)
        4. Número: Optimization Rate %
        5. Gráfico de linha: tendência de custo (atual vs anterior)
        6. Gráfico de barras: waste percentage por tipo
        7. Tabela: top 10 findings pendentes por priorityScore
        8. Gráfico de pizza: cost savings por serviço AWS

        Args:
            metrics: Resultado de calculate_metrics().
            cost_data: Resultado de fetch_cost_data().

        Returns:
            str: JSON serializado do dashboard body.
        """
        env = self.environment
        region = self.region
        account_id_placeholder = "${AWS::AccountId}"

        # Widget 1 — Total Savings (número grande)
        w_savings = {
            "type": "metric",
            "x": 0, "y": 0, "width": 6, "height": 4,
            "properties": {
                "title": "💰 Total Savings (Remediados)",
                "view": "singleValue",
                "metrics": [[
                    "GreenOps", "CostSaved",
                    "Environment", env,
                    {"stat": "Sum", "period": 2592000, "label": "USD/mês economizado"},
                ]],
                "annotations": {
                    "horizontal": [{"value": 0, "label": "Baseline", "color": "#aaaaaa"}]
                },
            },
        }

        # Widget 2 — Carbon Avoided
        w_carbon = {
            "type": "metric",
            "x": 6, "y": 0, "width": 6, "height": 4,
            "properties": {
                "title": "🌱 Carbon Avoided (MTCO2e)",
                "view": "singleValue",
                "metrics": [[
                    "GreenOps", "CarbonReduced",
                    "Environment", env,
                    {"stat": "Sum", "period": 2592000, "label": "MTCO2e evitado"},
                ]],
            },
        }

        # Widget 3 — Resources Optimized
        w_optimized = {
            "type": "metric",
            "x": 12, "y": 0, "width": 6, "height": 4,
            "properties": {
                "title": "✅ Recursos Otimizados",
                "view": "singleValue",
                "metrics": [[
                    "GreenOps/Remediation", "RemediationsApplied",
                    "Environment", env,
                    {"stat": "Sum", "period": 2592000, "label": "Remediações aplicadas"},
                ]],
            },
        }

        # Widget 4 — Optimization Rate (texto com valor calculado)
        opt_rate = metrics.get("optimization_rate", 0.0)
        trees = metrics.get("trees_equivalent", 0)
        cars = metrics.get("cars_equivalent", 0.0)
        w_rate = {
            "type": "text",
            "x": 18, "y": 0, "width": 6, "height": 4,
            "properties": {
                "markdown": (
                    f"## 📊 Taxa de Otimização\n\n"
                    f"**{opt_rate:.1f}%** dos findings remediados\n\n"
                    f"🌳 **{trees}** árvores equivalentes\n\n"
                    f"🚗 **{cars:.1f}** carros removidos"
                ),
            },
        }

        # Widget 5 — Tendência de custo (linha)
        w_cost_trend = {
            "type": "metric",
            "x": 0, "y": 4, "width": 12, "height": 6,
            "properties": {
                "title": "📈 Tendência de Custo AWS (USD)",
                "view": "timeSeries",
                "stacked": False,
                "metrics": [
                    [
                        "AWS/Billing", "EstimatedCharges",
                        "Currency", "USD",
                        {"stat": "Maximum", "period": 86400, "label": "Custo Estimado"},
                    ],
                    [
                        "GreenOps", "CostSaved",
                        "Environment", env,
                        {"stat": "Sum", "period": 86400, "label": "Economia GreenOps", "color": "#2ca02c"},
                    ],
                ],
                "annotations": {
                    "horizontal": [
                        {
                            "value": cost_data.get("previous_month_cost", 0),
                            "label": f"Mês anterior: {format_currency(cost_data.get('previous_month_cost', 0))}",
                            "color": "#ff7f0e",
                        }
                    ]
                },
                "period": 86400,
            },
        }

        # Widget 6 — Waste percentage por tipo (barras)
        waste_pct = metrics.get("waste_percentage", {})
        waste_bar_metrics = []
        for waste_type, pct in waste_pct.items():
            waste_bar_metrics.append([
                "GreenOps/Discovery", "FindingsCount",
                "Environment", env,
                "WasteType", waste_type,
                {"stat": "Sum", "period": 2592000, "label": f"{waste_type} ({pct:.1f}%)"},
            ])

        w_waste_bars = {
            "type": "metric",
            "x": 12, "y": 4, "width": 12, "height": 6,
            "properties": {
                "title": "🗂️ Findings por Tipo de Desperdício",
                "view": "bar",
                "metrics": waste_bar_metrics or [[
                    "GreenOps/Discovery", "FindingsCount",
                    "Environment", env,
                    {"stat": "Sum", "period": 2592000, "label": "Total Findings"},
                ]],
                "period": 2592000,
            },
        }

        # Widget 7 — Top 10 findings pendentes (tabela em markdown)
        top_pending = metrics.get("top_pending_findings", [])
        table_rows = ["| # | Recurso | Tipo | Ação | Savings | Score |", "|---|---------|------|------|---------|-------|"]
        for i, f in enumerate(top_pending[:TOP_PENDING_FINDINGS], 1):
            rec = f.get("recommendation", {})
            resource_id = f.get("resourceId", "")[:20]  # trunca para caber
            resource_type = f.get("resourceType", "").split("::")[-1]
            action = rec.get("action", "")
            savings = _safe_float(rec.get("estimatedMonthlySavings", 0))
            score = _safe_float(f.get("priorityScore", 0))
            table_rows.append(
                f"| {i} | `{resource_id}` | {resource_type} | {action} | "
                f"{format_currency(savings)} | {score:.1f} |"
            )

        w_top_pending = {
            "type": "text",
            "x": 0, "y": 10, "width": 24, "height": 8,
            "properties": {
                "markdown": (
                    "## 🔴 Top 10 Findings Pendentes (por PriorityScore)\n\n"
                    + "\n".join(table_rows)
                    + (
                        f"\n\n*{len(top_pending)} findings aguardando remediação — "
                        f"economia potencial: {format_currency(metrics.get('potential_savings', 0))}/mês*"
                    )
                ),
            },
        }

        # Widget 8 — Cost savings por serviço (pizza via barras horizontais)
        by_service = metrics.get("by_resource_type", {})
        service_metrics = []
        for svc, sav in sorted(by_service.items(), key=lambda x: x[1], reverse=True)[:8]:
            service_metrics.append([
                "GreenOps/Discovery", "FindingsCount",
                "Environment", env,
                {"stat": "Sum", "period": 2592000, "label": f"{svc}: {format_currency(sav)}"},
            ])

        w_cost_by_service = {
            "type": "metric",
            "x": 0, "y": 18, "width": 24, "height": 6,
            "properties": {
                "title": "🥧 Savings Potenciais por Serviço AWS",
                "view": "bar",
                "metrics": service_metrics or [[
                    "GreenOps/Discovery", "FindingsCount",
                    "Environment", env,
                    {"stat": "Sum", "period": 2592000, "label": "Findings"},
                ]],
                "period": 2592000,
            },
        }

        dashboard = {
            "widgets": [
                w_savings, w_carbon, w_optimized, w_rate,
                w_cost_trend, w_waste_bars,
                w_top_pending,
                w_cost_by_service,
            ]
        }

        return json.dumps(dashboard)

    def update_dashboard(
        self,
        metrics: dict[str, Any],
        cost_data: dict[str, float],
    ) -> bool:
        """
        Cria ou atualiza o CloudWatch Dashboard executivo.

        Args:
            metrics: Resultado de calculate_metrics().
            cost_data: Resultado de fetch_cost_data().

        Returns:
            bool: True se atualizado com sucesso.
        """
        try:
            dashboard_body = self.build_dashboard_body(metrics, cost_data)
            self.cw.put_dashboard(
                DashboardName=DASHBOARD_NAME,
                DashboardBody=dashboard_body,
            )
            print(f"[INFO] update_dashboard: dashboard '{DASHBOARD_NAME}' atualizado")
            return True
        except ClientError as exc:
            print(f"[ERROR] update_dashboard: falha ao atualizar dashboard: {exc}")
            return False

    # -------------------------------------------------------------------------
    # 5. Métricas CloudWatch customizadas
    # -------------------------------------------------------------------------

    def publish_metrics(
        self,
        metrics: dict[str, Any],
        cost_data: dict[str, float],
    ) -> None:
        """
        Publica métricas customizadas no namespace GreenOps/Reporting.

        Métricas publicadas:
        - TotalFindings, RemediatedCount, PendingCount
        - OptimizationRate, TotalSavings, TotalCarbonReduction
        - PotentialSavings, TreesEquivalent, CarsEquivalent
        - CurrentMonthCost, PreviousMonthCost, CostTrend
        """
        try:
            timestamp = _now_utc()
            env = self.environment

            metric_data = [
                {"MetricName": "TotalFindings",         "Value": metrics["total_findings"],         "Unit": "Count"},
                {"MetricName": "RemediatedCount",       "Value": metrics["remediated_count"],       "Unit": "Count"},
                {"MetricName": "PendingCount",          "Value": metrics["pending_count"],          "Unit": "Count"},
                {"MetricName": "FailedCount",           "Value": metrics["failed_count"],           "Unit": "Count"},
                {"MetricName": "OptimizationRate",      "Value": metrics["optimization_rate"],      "Unit": "Percent"},
                {"MetricName": "TotalSavings",          "Value": metrics["total_savings"],          "Unit": "None"},
                {"MetricName": "TotalCarbonReduction",  "Value": metrics["total_carbon_reduction"], "Unit": "None"},
                {"MetricName": "PotentialSavings",      "Value": metrics["potential_savings"],      "Unit": "None"},
                {"MetricName": "TreesEquivalent",       "Value": float(metrics["trees_equivalent"]), "Unit": "Count"},
                {"MetricName": "CarsEquivalent",        "Value": metrics["cars_equivalent"],        "Unit": "None"},
                {"MetricName": "CurrentMonthCost",      "Value": cost_data["current_month_cost"],   "Unit": "None"},
                {"MetricName": "PreviousMonthCost",     "Value": cost_data["previous_month_cost"],  "Unit": "None"},
                {"MetricName": "CostTrend",             "Value": cost_data["cost_trend"],           "Unit": "Percent"},
            ]

            # Adiciona dimensão Environment a todas as métricas
            for m in metric_data:
                m["Dimensions"] = [{"Name": "Environment", "Value": env}]
                m["Timestamp"] = timestamp

            # CloudWatch aceita máx 20 métricas por chamada
            chunk_size = 20
            for i in range(0, len(metric_data), chunk_size):
                self.cw.put_metric_data(
                    Namespace="GreenOps/Reporting",
                    MetricData=metric_data[i:i + chunk_size],
                )

            print(f"[INFO] publish_metrics: {len(metric_data)} métricas publicadas em GreenOps/Reporting")

        except Exception as exc:
            print(f"[ERROR] publish_metrics: falha ao publicar métricas: {exc}")

    # -------------------------------------------------------------------------
    # 6. Email executivo via SNS
    # -------------------------------------------------------------------------

    def send_executive_email(
        self,
        metrics: dict[str, Any],
        cost_data: dict[str, float],
        report_date: str,
    ) -> bool:
        """
        Envia email executivo semanal via SNS com resumo de savings e carbono.

        Formato do email:
        - Assunto: "GreenOps Report - YYYY-MM-DD"
        - Corpo: texto estruturado com KPIs, top 3 ações e alertas

        Args:
            metrics: Resultado de calculate_metrics().
            cost_data: Resultado de fetch_cost_data().
            report_date: Data do relatório no formato YYYY-MM-DD.

        Returns:
            bool: True se enviado com sucesso.
        """
        if not self.notifications_topic:
            print("[WARN] send_executive_email: NOTIFICATIONS_TOPIC não configurado")
            return False

        try:
            # Top 3 ações por savings
            top_pending = metrics.get("top_pending_findings", [])
            top3 = top_pending[:TOP_ACTIONS_EMAIL]

            top3_lines = []
            for i, f in enumerate(top3, 1):
                rec = f.get("recommendation", {})
                resource_id = f.get("resourceId", "")
                resource_type = f.get("resourceType", "").split("::")[-1]
                action = rec.get("action", "")
                savings = _safe_float(rec.get("estimatedMonthlySavings", 0))
                score = _safe_float(f.get("priorityScore", 0))
                top3_lines.append(
                    f"  {i}. [{action}] {resource_type}/{resource_id} "
                    f"— {format_currency(savings)}/mês (score: {score:.1f})"
                )

            # Alertas
            alerts: list[str] = []
            if metrics["failed_count"] > 0:
                alerts.append(f"⚠️  {metrics['failed_count']} remediações falharam — verificar logs")
            if metrics["rate_limited_count"] > 0:
                alerts.append(f"⚠️  {metrics['rate_limited_count']} ações bloqueadas por rate limit")
            if cost_data["cost_trend"] > 10:
                alerts.append(
                    f"📈 Custo AWS aumentou {cost_data['cost_trend']:+.1f}% vs mês anterior"
                )
            if metrics["optimization_rate"] < 20 and metrics["total_findings"] > 0:
                alerts.append(
                    f"🔴 Taxa de otimização baixa: {metrics['optimization_rate']:.1f}% "
                    f"({metrics['remediated_count']}/{metrics['total_findings']} findings)"
                )

            alerts_section = (
                "\n".join(alerts) if alerts
                else "  ✅ Nenhum alerta crítico neste período"
            )

            # Corpo do email
            body = f"""
╔══════════════════════════════════════════════════════════════╗
║           GreenOps Auto-Remediador — Relatório Executivo     ║
║                        {report_date}                         ║
╚══════════════════════════════════════════════════════════════╝

📊 RESUMO EXECUTIVO
───────────────────
  • Total de findings:        {metrics['total_findings']:>6}
  • Remediados:               {metrics['remediated_count']:>6}  ({metrics['optimization_rate']:.1f}% de otimização)
  • Pendentes:                {metrics['pending_count']:>6}
  • Falhas:                   {metrics['failed_count']:>6}

💰 IMPACTO FINANCEIRO
─────────────────────
  • Economia realizada:       {format_currency(metrics['total_savings'])}/mês
  • Economia potencial:       {format_currency(metrics['potential_savings'])}/mês
  • Custo AWS atual:          {format_currency(cost_data['current_month_cost'])}/mês
  • Custo AWS anterior:       {format_currency(cost_data['previous_month_cost'])}/mês
  • Tendência de custo:       {cost_data['cost_trend']:+.1f}%

🌱 IMPACTO AMBIENTAL
────────────────────
  • CO₂ evitado:              {format_carbon(metrics['total_carbon_reduction'])}
  • Equivalente em árvores:   {metrics['trees_equivalent']} árvores plantadas
  • Equivalente em carros:    {metrics['cars_equivalent']:.1f} carros removidos das ruas

🔝 TOP {TOP_ACTIONS_EMAIL} AÇÕES PRIORITÁRIAS
{'─' * 40}
{chr(10).join(top3_lines) if top3_lines else '  Nenhuma ação pendente no momento.'}

🚨 ALERTAS
──────────
{alerts_section}

───────────────────────────────────────────────────────────────
Relatório gerado automaticamente pelo GreenOps Auto-Remediador
Ambiente: {self.environment.upper()} | Região: {self.region}
Para detalhes completos, acesse o CloudWatch Dashboard:
https://{self.region}.console.aws.amazon.com/cloudwatch/home#dashboards:name={DASHBOARD_NAME}
""".strip()

            subject = f"GreenOps Report - {report_date}"[:100]

            self.sns.publish(
                TopicArn=self.notifications_topic,
                Subject=subject,
                Message=body,
            )
            print(f"[INFO] send_executive_email: email enviado para {self.notifications_topic}")
            return True

        except ClientError as exc:
            print(f"[ERROR] send_executive_email: falha ao enviar SNS: {exc}")
            return False

    # -------------------------------------------------------------------------
    # 7. Salvar relatório JSON no S3
    # -------------------------------------------------------------------------

    def save_report_to_s3(
        self,
        metrics: dict[str, Any],
        cost_data: dict[str, float],
        report_date: str,
    ) -> str | None:
        """
        Serializa e salva o relatório completo como JSON no S3.

        Chave S3: ``reports/YYYY/MM/DD/report.json``

        Args:
            metrics: Resultado de calculate_metrics().
            cost_data: Resultado de fetch_cost_data().
            report_date: Data no formato YYYY-MM-DD.

        Returns:
            str: S3 URI do relatório salvo (s3://bucket/key), ou None em caso de erro.
        """
        if not self.reports_bucket:
            print("[WARN] save_report_to_s3: REPORTS_BUCKET não configurado")
            return None

        try:
            # Monta a chave S3 com particionamento por data
            date_parts = report_date.split("-")
            year, month, day = date_parts[0], date_parts[1], date_parts[2]
            s3_key = f"reports/{year}/{month}/{day}/report.json"

            # Serializa métricas — converte Decimal para float
            def _serialize(obj: Any) -> Any:
                if isinstance(obj, Decimal):
                    return float(obj)
                if isinstance(obj, datetime):
                    return obj.isoformat()
                return obj

            report_payload = {
                "reportDate": report_date,
                "generatedAt": _iso_now(),
                "environment": self.environment,
                "region": self.region,
                "metrics": metrics,
                "costData": cost_data,
                "version": "1.0.0",
            }

            # Remove top_pending_findings do payload S3 (muito grande)
            # Mantém apenas os IDs para referência
            if "top_pending_findings" in report_payload["metrics"]:
                top = report_payload["metrics"]["top_pending_findings"]
                report_payload["metrics"]["top_pending_finding_ids"] = [
                    f.get("findingId", "") for f in top
                ]
                del report_payload["metrics"]["top_pending_findings"]

            report_json = json.dumps(report_payload, default=_serialize, indent=2)

            self.s3.put_object(
                Bucket=self.reports_bucket,
                Key=s3_key,
                Body=report_json.encode("utf-8"),
                ContentType="application/json",
                ServerSideEncryption="aws:kms",
                Metadata={
                    "environment": self.environment,
                    "report-date": report_date,
                    "total-findings": str(metrics.get("total_findings", 0)),
                    "total-savings": str(metrics.get("total_savings", 0)),
                },
            )

            s3_uri = f"s3://{self.reports_bucket}/{s3_key}"
            print(f"[INFO] save_report_to_s3: relatório salvo em {s3_uri}")
            return s3_uri

        except ClientError as exc:
            print(f"[ERROR] save_report_to_s3: falha ao salvar no S3: {exc}")
            return None

    # -------------------------------------------------------------------------
    # Orquestração principal
    # -------------------------------------------------------------------------

    def generate_report(self) -> dict[str, Any]:
        """
        Executa o pipeline completo de geração de relatório.

        Fluxo:
        1. Lê findings do DynamoDB
        2. Calcula métricas agregadas
        3. Busca dados de custo do Cost Explorer
        4. Atualiza CloudWatch Dashboard
        5. Publica métricas customizadas
        6. Envia email executivo via SNS
        7. Salva relatório JSON no S3

        Returns:
            dict com resultado de cada etapa e métricas calculadas.
        """
        report_date = _now_utc().strftime("%Y-%m-%d")
        print(f"[INFO] generate_report: iniciando relatório para {report_date}")

        result: dict[str, Any] = {
            "reportDate": report_date,
            "timestamp": _iso_now(),
            "environment": self.environment,
            "steps": {},
        }

        # 1. Findings
        findings = self.fetch_all_findings()
        result["steps"]["fetch_findings"] = {"count": len(findings)}

        # 2. Métricas
        metrics = self.calculate_metrics(findings)
        result["metrics"] = {
            k: v for k, v in metrics.items()
            if k != "top_pending_findings"  # não serializa lista completa
        }
        result["steps"]["calculate_metrics"] = {"ok": True}

        # 3. Cost Explorer
        cost_data = self.fetch_cost_data()
        result["costData"] = cost_data
        result["steps"]["fetch_cost_data"] = {"ok": True}

        # 4. Dashboard
        dashboard_ok = self.update_dashboard(metrics, cost_data)
        result["steps"]["update_dashboard"] = {"ok": dashboard_ok}

        # 5. Métricas CloudWatch
        try:
            self.publish_metrics(metrics, cost_data)
            result["steps"]["publish_metrics"] = {"ok": True}
        except Exception as exc:
            print(f"[ERROR] generate_report: publish_metrics falhou: {exc}")
            result["steps"]["publish_metrics"] = {"ok": False, "error": str(exc)}

        # 6. Email executivo
        email_ok = self.send_executive_email(metrics, cost_data, report_date)
        result["steps"]["send_email"] = {"ok": email_ok}

        # 7. S3
        s3_uri = self.save_report_to_s3(metrics, cost_data, report_date)
        result["steps"]["save_to_s3"] = {"ok": s3_uri is not None, "uri": s3_uri}

        print(
            f"[INFO] generate_report: concluído — "
            f"{metrics['total_findings']} findings, "
            f"{format_currency(metrics['total_savings'])}/mês economizado, "
            f"{format_carbon(metrics['total_carbon_reduction'])} evitado"
        )

        return result


# =============================================================================
# Lambda entry point — triggerado por EventBridge schedule
# =============================================================================

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Entry point da função Lambda de Reporting.

    Triggerado por EventBridge schedule (semanal, toda segunda às 08:00 UTC).
    Também pode ser invocado manualmente para gerar relatório ad-hoc.

    Evento EventBridge esperado::

        {
          "source": "eventbridge-schedule",
          "action": "generate-daily-report",
          "environment": "dev"   # opcional — sobrescreve ENVIRONMENT env var
        }

    Args:
        event: Evento EventBridge ou invocação manual.
        context: Contexto Lambda.

    Returns:
        dict com statusCode 200 e body JSON contendo:
        - reportDate: data do relatório
        - metrics: métricas calculadas (sem top_pending_findings)
        - costData: dados de custo do Cost Explorer
        - steps: resultado de cada etapa do pipeline
        - timestamp: timestamp da execução
    """
    print(f"[INFO] lambda_handler: iniciando reporting — event={json.dumps(event)}")

    region = os.environ.get("AWS_REGION", "us-east-1")

    generator = ReportingGenerator(region=region)

    try:
        result = generator.generate_report()
        return {
            "statusCode": 200,
            "body": json.dumps(result, default=str),
        }
    except Exception as exc:
        print(f"[ERROR] lambda_handler: falha inesperada: {exc}")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": str(exc),
                "timestamp": _iso_now(),
            }),
        }
