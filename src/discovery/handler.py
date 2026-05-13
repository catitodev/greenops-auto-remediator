"""
GreenOps Auto-Remediador — Discovery Handler
============================================
Orquestra a varredura de recursos AWS desperdiçados em múltiplos serviços,
persiste findings no DynamoDB e publica métricas no CloudWatch.

Entry point Lambda: ``lambda_handler(event, context)``

Recursos escaneados:
- EC2 instances  — idle (CPU < 5% / 7 dias) e oversized (Compute Optimizer)
- EBS volumes    — orphaned (desanexados por 30+ dias)
- Elastic IPs    — unassociated
- RDS instances  — idle (DatabaseConnections < 1 / 7 dias)
- Lambda funcs   — oversized memory (alocada > 3x usada)
- S3 buckets     — misconfigured (sem lifecycle policy)
- Load Balancers — idle (0 healthy hosts / 7+ dias)

Filtros de segurança aplicados em todos os scans:
- Apenas recursos com tag ``GreenOpsManaged=true``
- Recursos com tag ``Environment=Production`` são marcados como protegidos
  (findings gerados, mas riskLevel elevado para CRITICAL)
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Imports do módulo shared — caminhos relativos ao PYTHONPATH da Lambda Layer
from shared.constants import ActionType, RiskLevel, Severity, WasteType
from shared.utils import calculate_priority_score, parse_tags

# =============================================================================
# Constantes de thresholds
# =============================================================================

# EC2 — CPU médio abaixo deste valor por IDLE_DAYS_THRESHOLD = idle
EC2_CPU_IDLE_THRESHOLD_PCT: float = 5.0
IDLE_DAYS_THRESHOLD: int = 7

# EBS — volume desanexado há mais de N dias = orphaned
EBS_ORPHAN_DAYS_THRESHOLD: int = 30

# RDS — conexões abaixo deste valor por IDLE_DAYS_THRESHOLD = idle
RDS_CONNECTIONS_IDLE_THRESHOLD: float = 1.0

# Lambda — memória alocada > N * memória usada (p99) = oversized
LAMBDA_MEMORY_OVERSIZED_RATIO: float = 3.0

# Load Balancer — healthy hosts abaixo deste valor por IDLE_DAYS_THRESHOLD = idle
ALB_HEALTHY_HOSTS_THRESHOLD: float = 0.0

# Estimativas de custo e carbono por tipo de recurso (USD/mês e MTCO2e/mês)
# Valores conservadores baseados em preços AWS us-east-1 e fatores EPA.
# O módulo de reporting refina esses valores com dados reais do Cost Explorer.
_COST_ESTIMATES: dict[str, dict[str, float]] = {
    "ec2_idle_t3_medium":    {"savings": 30.0,  "carbon": 0.05},
    "ec2_idle_m5_large":     {"savings": 70.0,  "carbon": 0.12},
    "ec2_idle_default":      {"savings": 50.0,  "carbon": 0.08},
    "ec2_oversized":         {"savings": 40.0,  "carbon": 0.07},
    "ebs_orphan_gp2_100gb":  {"savings": 10.0,  "carbon": 0.01},
    "ebs_orphan_default":    {"savings": 8.0,   "carbon": 0.01},
    "eip_unassociated":      {"savings": 3.6,   "carbon": 0.001},
    "rds_idle_db_t3_medium": {"savings": 50.0,  "carbon": 0.09},
    "rds_idle_default":      {"savings": 80.0,  "carbon": 0.14},
    "lambda_oversized":      {"savings": 5.0,   "carbon": 0.005},
    "s3_no_lifecycle":       {"savings": 15.0,  "carbon": 0.02},
    "alb_idle":              {"savings": 20.0,  "carbon": 0.03},
}


# =============================================================================
# Helpers internos
# =============================================================================

def _now_utc() -> datetime:
    """Retorna o datetime atual em UTC com timezone-aware."""
    return datetime.now(tz=timezone.utc)


def _iso_now() -> str:
    """Retorna timestamp ISO 8601 atual em UTC."""
    return _now_utc().isoformat()


def _days_ago(days: int) -> datetime:
    """Retorna datetime N dias atrás em UTC."""
    return _now_utc() - timedelta(days=days)


def _make_finding_id(resource_type: str, resource_id: str) -> str:
    """
    Gera um findingId determinístico baseado no tipo e ID do recurso.
    Usa UUID v5 (namespace + nome) para garantir idempotência:
    o mesmo recurso sempre gera o mesmo findingId no mesmo dia.
    """
    date_str = _now_utc().strftime("%Y-%m-%d")
    name = f"{resource_type}:{resource_id}:{date_str}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


def _is_production_resource(tags: dict[str, str]) -> bool:
    """Retorna True se o recurso tem tag Environment=Production."""
    return tags.get("Environment", "").lower() == "production"


def _get_cloudwatch_average(
    cw_client: Any,
    namespace: str,
    metric_name: str,
    dimensions: list[dict[str, str]],
    days: int,
    stat: str = "Average",
) -> float | None:
    """
    Busca a média de uma métrica CloudWatch nos últimos N dias.

    Returns:
        float com o valor médio, ou None se não houver dados suficientes.
    """
    try:
        end_time = _now_utc()
        start_time = end_time - timedelta(days=days)

        response = cw_client.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=start_time,
            EndTime=end_time,
            Period=days * 86400,  # período = janela inteira → 1 datapoint
            Statistics=[stat],
        )

        datapoints = response.get("Datapoints", [])
        if not datapoints:
            return None

        # Retorna o valor do único datapoint (período = janela completa)
        return float(datapoints[0].get(stat, 0.0))

    except ClientError as exc:
        print(f"[WARN] CloudWatch get_metric_statistics falhou: {exc}")
        return None


def _build_finding(
    resource_type: str,
    resource_id: str,
    region: str,
    waste_type: WasteType,
    severity: Severity,
    description: str,
    metrics: dict[str, Any],
    action: ActionType,
    reason: str,
    estimated_savings: float,
    estimated_carbon: float,
    risk_level: RiskLevel,
    tags: dict[str, str],
    confidence: float = 0.8,
) -> dict[str, Any]:
    """
    Constrói um finding no formato padrão do GreenOps.

    Todos os campos são serializáveis em JSON e compatíveis com DynamoDB
    (floats convertidos para Decimal onde necessário pelo caller).
    """
    finding_id = _make_finding_id(resource_type, resource_id)
    priority_score = calculate_priority_score(
        monthly_savings=estimated_savings,
        carbon_mtco2e=estimated_carbon,
        severity_weight=severity.weight,
        confidence=confidence,
    )

    return {
        "findingId": finding_id,
        "timestamp": _iso_now(),
        "resourceType": resource_type,
        "resourceId": resource_id,
        "region": region,
        "wasteType": waste_type.value,
        "severity": severity.value,
        "description": description,
        "metrics": metrics,
        "recommendation": {
            "action": action.value,
            "reason": reason,
            "estimatedMonthlySavings": estimated_savings,
            "estimatedMonthlyCarbonReduction": estimated_carbon,
            "riskLevel": risk_level.value,
        },
        "tags": tags,
        "priorityScore": priority_score,
        # TTL DynamoDB: findings expiram após FINDINGS_TTL_DAYS dias
        "ttl": int(
            (_now_utc() + timedelta(days=int(os.environ.get("FINDINGS_TTL_DAYS", "90")))).timestamp()
        ),
    }


def _to_dynamodb_item(finding: dict[str, Any]) -> dict[str, Any]:
    """
    Converte floats para Decimal para compatibilidade com DynamoDB.
    DynamoDB não aceita float nativo do Python — requer Decimal.
    """
    def _convert(obj: Any) -> Any:
        if isinstance(obj, float):
            return Decimal(str(obj))
        if isinstance(obj, dict):
            return {k: _convert(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_convert(i) for i in obj]
        return obj

    return _convert(finding)


# =============================================================================
# DiscoveryOrchestrator
# =============================================================================

class DiscoveryOrchestrator:
    """
    Orquestra a varredura de recursos AWS desperdiçados em múltiplos serviços.

    Cada método ``scan_*`` é independente e isolado com try/except — uma falha
    em um serviço não interrompe os demais scans.

    Args:
        region: Região AWS a escanear. Padrão: variável AWS_REGION ou us-east-1.
        session: Sessão boto3 customizada (útil para testes com moto).
    """

    def __init__(
        self,
        region: str | None = None,
        session: boto3.Session | None = None,
    ) -> None:
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._session = session or boto3.Session(region_name=self.region)

        # Lazy-initialized clients — criados apenas quando o scan é executado
        self._ec2: Any = None
        self._rds: Any = None
        self._cw: Any = None
        self._co: Any = None   # Compute Optimizer
        self._s3: Any = None
        self._elb: Any = None  # ELBv2 (ALB/NLB)

    # -------------------------------------------------------------------------
    # Clients (lazy)
    # -------------------------------------------------------------------------

    @property
    def ec2(self) -> Any:
        if self._ec2 is None:
            self._ec2 = self._session.client("ec2", region_name=self.region)
        return self._ec2

    @property
    def rds(self) -> Any:
        if self._rds is None:
            self._rds = self._session.client("rds", region_name=self.region)
        return self._rds

    @property
    def cw(self) -> Any:
        if self._cw is None:
            self._cw = self._session.client("cloudwatch", region_name=self.region)
        return self._cw

    @property
    def co(self) -> Any:
        if self._co is None:
            self._co = self._session.client("compute-optimizer", region_name=self.region)
        return self._co

    @property
    def s3(self) -> Any:
        if self._s3 is None:
            self._s3 = self._session.client("s3", region_name=self.region)
        return self._s3

    @property
    def elb(self) -> Any:
        if self._elb is None:
            self._elb = self._session.client("elbv2", region_name=self.region)
        return self._elb

    # -------------------------------------------------------------------------
    # Scan: EC2 instances
    # -------------------------------------------------------------------------

    def scan_ec2_instances(self) -> list[dict[str, Any]]:
        """
        Escaneia instâncias EC2 em busca de recursos idle e oversized.

        Critérios:
        - IDLE: CPUUtilization média < 5% nos últimos 7 dias (instâncias running)
        - OVERSIZED: Compute Optimizer recomenda tipo menor

        Filtros:
        - Apenas instâncias com tag GreenOpsManaged=true
        - Apenas instâncias no estado 'running'

        Returns:
            Lista de findings no formato padrão.
        """
        findings: list[dict[str, Any]] = []

        try:
            print("[INFO] scan_ec2_instances: iniciando")

            # Busca instâncias running com tag GreenOpsManaged=true
            paginator = self.ec2.get_paginator("describe_instances")
            pages = paginator.paginate(
                Filters=[
                    {"Name": "instance-state-name", "Values": ["running"]},
                    {"Name": "tag:GreenOpsManaged",  "Values": ["true"]},
                ]
            )

            instances: list[dict[str, Any]] = []
            for page in pages:
                for reservation in page.get("Reservations", []):
                    instances.extend(reservation.get("Instances", []))

            print(f"[INFO] scan_ec2_instances: {len(instances)} instâncias encontradas")

            # Busca recomendações do Compute Optimizer em lote
            co_recommendations: dict[str, dict[str, Any]] = {}
            try:
                instance_arns = [
                    f"arn:aws:ec2:{self.region}:{inst.get('OwnerId', '')}:instance/{inst['InstanceId']}"
                    for inst in instances
                ]
                if instance_arns:
                    co_resp = self.co.get_ec2_instance_recommendations(
                        instanceArns=instance_arns[:100]  # limite da API
                    )
                    for rec in co_resp.get("instanceRecommendations", []):
                        iid = rec["instanceArn"].split("/")[-1]
                        co_recommendations[iid] = rec
            except ClientError as exc:
                print(f"[WARN] scan_ec2_instances: Compute Optimizer indisponível: {exc}")

            for inst in instances:
                instance_id = inst["InstanceId"]
                instance_type = inst.get("InstanceType", "unknown")
                tags = parse_tags(inst.get("Tags", []))

                # --- Scan IDLE ---
                try:
                    avg_cpu = _get_cloudwatch_average(
                        cw_client=self.cw,
                        namespace="AWS/EC2",
                        metric_name="CPUUtilization",
                        dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                        days=IDLE_DAYS_THRESHOLD,
                    )

                    if avg_cpu is not None and avg_cpu < EC2_CPU_IDLE_THRESHOLD_PCT:
                        # Estima custo baseado no tipo de instância
                        cost_key = (
                            "ec2_idle_t3_medium" if "t3.medium" in instance_type
                            else "ec2_idle_m5_large" if "m5.large" in instance_type
                            else "ec2_idle_default"
                        )
                        est = _COST_ESTIMATES[cost_key]

                        # Recursos de produção recebem riskLevel CRITICAL
                        is_prod = _is_production_resource(tags)
                        risk = RiskLevel.CRITICAL if is_prod else RiskLevel.MEDIUM
                        severity = Severity.CRITICAL if is_prod else Severity.HIGH

                        finding = _build_finding(
                            resource_type="AWS::EC2::Instance",
                            resource_id=instance_id,
                            region=self.region,
                            waste_type=WasteType.IDLE,
                            severity=severity,
                            description=(
                                f"Instância EC2 {instance_id} ({instance_type}) com CPU médio "
                                f"de {avg_cpu:.1f}% nos últimos {IDLE_DAYS_THRESHOLD} dias "
                                f"(threshold: {EC2_CPU_IDLE_THRESHOLD_PCT}%)"
                            ),
                            metrics={
                                "avgCpuUtilizationPct": avg_cpu,
                                "observationDays": IDLE_DAYS_THRESHOLD,
                                "instanceType": instance_type,
                            },
                            action=ActionType.STOP,
                            reason=(
                                f"Instância com utilização de CPU consistentemente abaixo de "
                                f"{EC2_CPU_IDLE_THRESHOLD_PCT}% por {IDLE_DAYS_THRESHOLD} dias. "
                                f"Candidata a stop ou terminação após validação."
                            ),
                            estimated_savings=est["savings"],
                            estimated_carbon=est["carbon"],
                            risk_level=risk,
                            tags=tags,
                            confidence=0.85,
                        )
                        findings.append(finding)
                        print(f"[INFO] scan_ec2_instances: IDLE finding para {instance_id} (CPU={avg_cpu:.1f}%)")

                except Exception as exc:
                    print(f"[ERROR] scan_ec2_instances: erro ao verificar idle para {instance_id}: {exc}")

                # --- Scan OVERSIZED (Compute Optimizer) ---
                try:
                    co_rec = co_recommendations.get(instance_id)
                    if co_rec and co_rec.get("finding") == "OVER_PROVISIONED":
                        options = co_rec.get("recommendationOptions", [])
                        best = options[0] if options else None
                        recommended_type = (
                            best.get("instanceType", "menor") if best else "menor"
                        )
                        perf_risk = best.get("performanceRisk", 0.5) if best else 0.5

                        est = _COST_ESTIMATES["ec2_oversized"]
                        is_prod = _is_production_resource(tags)
                        risk = RiskLevel.CRITICAL if is_prod else RiskLevel.HIGH

                        finding = _build_finding(
                            resource_type="AWS::EC2::Instance",
                            resource_id=instance_id,
                            region=self.region,
                            waste_type=WasteType.OVERSIZED,
                            severity=Severity.HIGH,
                            description=(
                                f"Instância EC2 {instance_id} ({instance_type}) superdimensionada. "
                                f"Compute Optimizer recomenda migrar para {recommended_type}."
                            ),
                            metrics={
                                "currentInstanceType": instance_type,
                                "recommendedInstanceType": recommended_type,
                                "performanceRisk": perf_risk,
                                "coFinding": co_rec.get("finding", ""),
                            },
                            action=ActionType.RESIZE,
                            reason=(
                                f"Compute Optimizer identificou superdimensionamento. "
                                f"Tipo recomendado: {recommended_type} "
                                f"(risco de performance: {perf_risk:.0%})."
                            ),
                            estimated_savings=est["savings"],
                            estimated_carbon=est["carbon"],
                            risk_level=risk,
                            tags=tags,
                            confidence=0.9,
                        )
                        findings.append(finding)
                        print(f"[INFO] scan_ec2_instances: OVERSIZED finding para {instance_id} → {recommended_type}")

                except Exception as exc:
                    print(f"[ERROR] scan_ec2_instances: erro ao verificar oversized para {instance_id}: {exc}")

        except Exception as exc:
            print(f"[ERROR] scan_ec2_instances: falha geral no scan: {exc}")

        print(f"[INFO] scan_ec2_instances: {len(findings)} findings gerados")
        return findings

    # -------------------------------------------------------------------------
    # Scan: EBS volumes
    # -------------------------------------------------------------------------

    def scan_ebs_volumes(self) -> list[dict[str, Any]]:
        """
        Escaneia volumes EBS desanexados (orphaned) há mais de 30 dias.

        Critérios:
        - Estado 'available' (não anexado a nenhuma instância)
        - CreateTime ou detach time > EBS_ORPHAN_DAYS_THRESHOLD dias atrás

        Filtros:
        - Apenas volumes com tag GreenOpsManaged=true

        Returns:
            Lista de findings no formato padrão.
        """
        findings: list[dict[str, Any]] = []

        try:
            print("[INFO] scan_ebs_volumes: iniciando")

            paginator = self.ec2.get_paginator("describe_volumes")
            pages = paginator.paginate(
                Filters=[
                    {"Name": "status",              "Values": ["available"]},
                    {"Name": "tag:GreenOpsManaged", "Values": ["true"]},
                ]
            )

            cutoff = _days_ago(EBS_ORPHAN_DAYS_THRESHOLD)
            count = 0

            for page in pages:
                for vol in page.get("Volumes", []):
                    volume_id = vol["VolumeId"]
                    tags = parse_tags(vol.get("Tags", []))

                    try:
                        create_time = vol.get("CreateTime")
                        if create_time and create_time.replace(tzinfo=timezone.utc) > cutoff:
                            # Volume criado recentemente — pode estar em processo de attach
                            continue

                        size_gb = vol.get("Size", 0)
                        volume_type = vol.get("VolumeType", "gp2")
                        iops = vol.get("Iops", 0)

                        # Custo estimado: gp2 = $0.10/GB/mês, gp3 = $0.08/GB/mês
                        cost_per_gb = 0.10 if volume_type == "gp2" else 0.08
                        estimated_savings = round(size_gb * cost_per_gb, 2)
                        estimated_carbon = round(size_gb * 0.0001, 4)  # ~0.1g CO2/GB/mês

                        est_key = "ebs_orphan_gp2_100gb" if size_gb >= 100 else "ebs_orphan_default"
                        # Usa estimativa calculada se mais precisa
                        if estimated_savings > 0:
                            pass  # usa o valor calculado acima

                        is_prod = _is_production_resource(tags)
                        risk = RiskLevel.CRITICAL if is_prod else RiskLevel.CRITICAL  # DELETE sempre CRITICAL

                        finding = _build_finding(
                            resource_type="AWS::EC2::Volume",
                            resource_id=volume_id,
                            region=self.region,
                            waste_type=WasteType.ORPHAN,
                            severity=Severity.MEDIUM,
                            description=(
                                f"Volume EBS {volume_id} ({volume_type}, {size_gb}GB) "
                                f"desanexado há mais de {EBS_ORPHAN_DAYS_THRESHOLD} dias."
                            ),
                            metrics={
                                "sizeGb": size_gb,
                                "volumeType": volume_type,
                                "iops": iops,
                                "orphanDaysThreshold": EBS_ORPHAN_DAYS_THRESHOLD,
                            },
                            action=ActionType.DELETE,
                            reason=(
                                f"Volume desanexado sem uso por mais de {EBS_ORPHAN_DAYS_THRESHOLD} dias. "
                                f"Recomenda-se criar snapshot de backup antes de deletar."
                            ),
                            estimated_savings=estimated_savings,
                            estimated_carbon=estimated_carbon,
                            risk_level=risk,
                            tags=tags,
                            confidence=0.95,
                        )
                        findings.append(finding)
                        count += 1
                        print(f"[INFO] scan_ebs_volumes: ORPHAN finding para {volume_id} ({size_gb}GB)")

                    except Exception as exc:
                        print(f"[ERROR] scan_ebs_volumes: erro ao processar volume {volume_id}: {exc}")

            print(f"[INFO] scan_ebs_volumes: {count} volumes orphaned encontrados")

        except Exception as exc:
            print(f"[ERROR] scan_ebs_volumes: falha geral no scan: {exc}")

        return findings

    # -------------------------------------------------------------------------
    # Scan: Elastic IPs
    # -------------------------------------------------------------------------

    def scan_elastic_ips(self) -> list[dict[str, Any]]:
        """
        Escaneia Elastic IPs não associados a nenhuma instância ou interface.

        Critérios:
        - AssociationId ausente (EIP não associado)
        - NetworkInterfaceId ausente

        Filtros:
        - Apenas EIPs com tag GreenOpsManaged=true

        Returns:
            Lista de findings no formato padrão.
        """
        findings: list[dict[str, Any]] = []

        try:
            print("[INFO] scan_elastic_ips: iniciando")

            response = self.ec2.describe_addresses(
                Filters=[{"Name": "tag:GreenOpsManaged", "Values": ["true"]}]
            )

            for addr in response.get("Addresses", []):
                allocation_id = addr.get("AllocationId", addr.get("PublicIp", "unknown"))
                public_ip = addr.get("PublicIp", "")
                tags = parse_tags(addr.get("Tags", []))

                # EIP não associado: sem AssociationId e sem NetworkInterfaceId
                if addr.get("AssociationId") or addr.get("NetworkInterfaceId"):
                    continue

                try:
                    est = _COST_ESTIMATES["eip_unassociated"]
                    is_prod = _is_production_resource(tags)
                    risk = RiskLevel.CRITICAL if is_prod else RiskLevel.HIGH

                    finding = _build_finding(
                        resource_type="AWS::EC2::EIP",
                        resource_id=allocation_id,
                        region=self.region,
                        waste_type=WasteType.ORPHAN,
                        severity=Severity.LOW,
                        description=(
                            f"Elastic IP {public_ip} ({allocation_id}) não está associado "
                            f"a nenhuma instância ou interface de rede."
                        ),
                        metrics={
                            "publicIp": public_ip,
                            "allocationId": allocation_id,
                            "domain": addr.get("Domain", "vpc"),
                        },
                        action=ActionType.RELEASE,
                        reason=(
                            "Elastic IP não associado gera custo de $0.005/hora (~$3.60/mês). "
                            "Liberar o EIP elimina esse custo imediatamente."
                        ),
                        estimated_savings=est["savings"],
                        estimated_carbon=est["carbon"],
                        risk_level=risk,
                        tags=tags,
                        confidence=1.0,  # certeza absoluta — EIP não associado é fato
                    )
                    findings.append(finding)
                    print(f"[INFO] scan_elastic_ips: ORPHAN finding para {public_ip} ({allocation_id})")

                except Exception as exc:
                    print(f"[ERROR] scan_elastic_ips: erro ao processar EIP {allocation_id}: {exc}")

        except Exception as exc:
            print(f"[ERROR] scan_elastic_ips: falha geral no scan: {exc}")

        print(f"[INFO] scan_elastic_ips: {len(findings)} findings gerados")
        return findings

    # -------------------------------------------------------------------------
    # Scan: RDS instances
    # -------------------------------------------------------------------------

    def scan_rds_instances(self) -> list[dict[str, Any]]:
        """
        Escaneia instâncias RDS idle (DatabaseConnections < 1 por 7 dias).

        Critérios:
        - DatabaseConnections média < 1 nos últimos 7 dias
        - Instância no estado 'available'

        Filtros:
        - Apenas instâncias com tag GreenOpsManaged=true

        Returns:
            Lista de findings no formato padrão.
        """
        findings: list[dict[str, Any]] = []

        try:
            print("[INFO] scan_rds_instances: iniciando")

            paginator = self.rds.get_paginator("describe_db_instances")
            pages = paginator.paginate()

            for page in pages:
                for db in page.get("DBInstances", []):
                    if db.get("DBInstanceStatus") != "available":
                        continue

                    db_id = db["DBInstanceIdentifier"]
                    db_arn = db.get("DBInstanceArn", "")

                    # Busca tags via ARN (RDS não retorna tags no describe)
                    try:
                        tags_resp = self.rds.list_tags_for_resource(ResourceName=db_arn)
                        tags = parse_tags(tags_resp.get("TagList", []))
                    except ClientError:
                        tags = {}

                    # Filtra por GreenOpsManaged=true
                    if tags.get("GreenOpsManaged", "").lower() != "true":
                        continue

                    try:
                        avg_connections = _get_cloudwatch_average(
                            cw_client=self.cw,
                            namespace="AWS/RDS",
                            metric_name="DatabaseConnections",
                            dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
                            days=IDLE_DAYS_THRESHOLD,
                        )

                        if avg_connections is None or avg_connections >= RDS_CONNECTIONS_IDLE_THRESHOLD:
                            continue

                        db_class = db.get("DBInstanceClass", "db.t3.medium")
                        engine = db.get("Engine", "mysql")
                        multi_az = db.get("MultiAZ", False)

                        cost_key = (
                            "rds_idle_db_t3_medium" if "t3.medium" in db_class
                            else "rds_idle_default"
                        )
                        est = _COST_ESTIMATES[cost_key]
                        # Multi-AZ dobra o custo estimado
                        if multi_az:
                            est = {k: v * 2 for k, v in est.items()}

                        is_prod = _is_production_resource(tags)
                        risk = RiskLevel.CRITICAL if is_prod else RiskLevel.MEDIUM
                        severity = Severity.HIGH if multi_az else Severity.MEDIUM

                        finding = _build_finding(
                            resource_type="AWS::RDS::DBInstance",
                            resource_id=db_id,
                            region=self.region,
                            waste_type=WasteType.IDLE,
                            severity=severity,
                            description=(
                                f"Instância RDS {db_id} ({db_class}, {engine}) com média de "
                                f"{avg_connections:.2f} conexões nos últimos {IDLE_DAYS_THRESHOLD} dias "
                                f"(threshold: {RDS_CONNECTIONS_IDLE_THRESHOLD})."
                            ),
                            metrics={
                                "avgDatabaseConnections": avg_connections,
                                "observationDays": IDLE_DAYS_THRESHOLD,
                                "dbInstanceClass": db_class,
                                "engine": engine,
                                "multiAz": multi_az,
                            },
                            action=ActionType.STOP,
                            reason=(
                                f"Instância RDS sem conexões ativas por {IDLE_DAYS_THRESHOLD} dias. "
                                f"Candidata a stop (RDS pode ser reiniciado quando necessário)."
                            ),
                            estimated_savings=est["savings"],
                            estimated_carbon=est["carbon"],
                            risk_level=risk,
                            tags=tags,
                            confidence=0.85,
                        )
                        findings.append(finding)
                        print(f"[INFO] scan_rds_instances: IDLE finding para {db_id} (connections={avg_connections:.2f})")

                    except Exception as exc:
                        print(f"[ERROR] scan_rds_instances: erro ao processar {db_id}: {exc}")

        except Exception as exc:
            print(f"[ERROR] scan_rds_instances: falha geral no scan: {exc}")

        print(f"[INFO] scan_rds_instances: {len(findings)} findings gerados")
        return findings

    # -------------------------------------------------------------------------
    # Scan: Lambda functions
    # -------------------------------------------------------------------------

    def scan_lambda_functions(self) -> list[dict[str, Any]]:
        """
        Escaneia funções Lambda com memória superdimensionada.

        Critérios:
        - Memória alocada > 3x a memória máxima usada (p99 de MemorySize nos últimos 7 dias)
        - Função com pelo menos 1 invocação no período (evita falsos positivos)

        Filtros:
        - Apenas funções com tag GreenOpsManaged=true

        Returns:
            Lista de findings no formato padrão.
        """
        findings: list[dict[str, Any]] = []

        try:
            print("[INFO] scan_lambda_functions: iniciando")

            lambda_client = self._session.client("lambda", region_name=self.region)
            paginator = lambda_client.get_paginator("list_functions")
            pages = paginator.paginate()

            for page in pages:
                for fn in page.get("Functions", []):
                    fn_name = fn["FunctionName"]
                    fn_arn = fn["FunctionArn"]
                    allocated_mb = fn.get("MemorySize", 128)

                    # Busca tags da função
                    try:
                        tags_resp = lambda_client.list_tags(Resource=fn_arn)
                        tags = tags_resp.get("Tags", {})
                    except ClientError:
                        tags = {}

                    if tags.get("GreenOpsManaged", "").lower() != "true":
                        continue

                    try:
                        # Verifica se houve invocações no período (evita falsos positivos)
                        invocations = _get_cloudwatch_average(
                            cw_client=self.cw,
                            namespace="AWS/Lambda",
                            metric_name="Invocations",
                            dimensions=[{"Name": "FunctionName", "Value": fn_name}],
                            days=IDLE_DAYS_THRESHOLD,
                            stat="Sum",
                        )

                        if not invocations or invocations < 1:
                            # Sem invocações — não há dados de uso de memória
                            continue

                        # Memória máxima usada (p99) via Lambda Insights ou estimativa
                        # Usa MaxMemoryUsed do CloudWatch Lambda Insights se disponível
                        max_memory_used = _get_cloudwatch_average(
                            cw_client=self.cw,
                            namespace="LambdaInsights",
                            metric_name="memory_utilization",
                            dimensions=[{"Name": "function_name", "Value": fn_name}],
                            days=IDLE_DAYS_THRESHOLD,
                            stat="Maximum",
                        )

                        if max_memory_used is None:
                            # Fallback: estima 33% de uso como heurística conservadora
                            max_memory_used = allocated_mb * 0.33

                        # Verifica ratio de superdimensionamento
                        if allocated_mb <= max_memory_used * LAMBDA_MEMORY_OVERSIZED_RATIO:
                            continue

                        # Recomenda o menor múltiplo de 64MB acima do uso máximo + 20% buffer
                        recommended_mb = max(
                            128,
                            int(max_memory_used * 1.2 / 64 + 1) * 64,
                        )

                        if recommended_mb >= allocated_mb:
                            continue  # Sem ganho real

                        # Custo Lambda: $0.0000166667/GB-segundo
                        # Economia proporcional à redução de memória
                        memory_reduction_ratio = 1 - (recommended_mb / allocated_mb)
                        est = _COST_ESTIMATES["lambda_oversized"]
                        estimated_savings = round(est["savings"] * memory_reduction_ratio, 2)
                        estimated_carbon = round(est["carbon"] * memory_reduction_ratio, 4)

                        is_prod = _is_production_resource(tags)
                        risk = RiskLevel.CRITICAL if is_prod else RiskLevel.HIGH

                        finding = _build_finding(
                            resource_type="AWS::Lambda::Function",
                            resource_id=fn_name,
                            region=self.region,
                            waste_type=WasteType.OVERSIZED,
                            severity=Severity.LOW,
                            description=(
                                f"Função Lambda {fn_name} com {allocated_mb}MB alocados mas "
                                f"usando no máximo {max_memory_used:.0f}MB "
                                f"(ratio: {allocated_mb/max_memory_used:.1f}x, threshold: {LAMBDA_MEMORY_OVERSIZED_RATIO}x)."
                            ),
                            metrics={
                                "allocatedMemoryMb": allocated_mb,
                                "maxMemoryUsedMb": round(max_memory_used, 1),
                                "recommendedMemoryMb": recommended_mb,
                                "oversizedRatio": round(allocated_mb / max_memory_used, 2),
                                "invocationsInPeriod": invocations,
                            },
                            action=ActionType.RESIZE,
                            reason=(
                                f"Memória alocada ({allocated_mb}MB) é {allocated_mb/max_memory_used:.1f}x "
                                f"maior que o uso máximo observado ({max_memory_used:.0f}MB). "
                                f"Reduzir para {recommended_mb}MB mantém 20% de buffer de segurança."
                            ),
                            estimated_savings=estimated_savings,
                            estimated_carbon=estimated_carbon,
                            risk_level=risk,
                            tags=tags,
                            confidence=0.75,
                        )
                        findings.append(finding)
                        print(f"[INFO] scan_lambda_functions: OVERSIZED finding para {fn_name} ({allocated_mb}MB → {recommended_mb}MB)")

                    except Exception as exc:
                        print(f"[ERROR] scan_lambda_functions: erro ao processar {fn_name}: {exc}")

        except Exception as exc:
            print(f"[ERROR] scan_lambda_functions: falha geral no scan: {exc}")

        print(f"[INFO] scan_lambda_functions: {len(findings)} findings gerados")
        return findings

    # -------------------------------------------------------------------------
    # Scan: S3 buckets
    # -------------------------------------------------------------------------

    def scan_s3_buckets(self) -> list[dict[str, Any]]:
        """
        Escaneia buckets S3 sem lifecycle policy configurada.

        Critérios:
        - Bucket sem nenhuma regra de lifecycle (GetBucketLifecycleConfiguration retorna NoSuchLifecycleConfiguration)

        Filtros:
        - Apenas buckets com tag GreenOpsManaged=true

        Returns:
            Lista de findings no formato padrão.
        """
        findings: list[dict[str, Any]] = []

        try:
            print("[INFO] scan_s3_buckets: iniciando")

            response = self.s3.list_buckets()
            buckets = response.get("Buckets", [])
            print(f"[INFO] scan_s3_buckets: {len(buckets)} buckets encontrados")

            for bucket in buckets:
                bucket_name = bucket["Name"]

                # Busca tags do bucket
                try:
                    tags_resp = self.s3.get_bucket_tagging(Bucket=bucket_name)
                    tags = parse_tags(tags_resp.get("TagSet", []))
                except ClientError as exc:
                    if exc.response["Error"]["Code"] == "NoSuchTagSet":
                        tags = {}
                    else:
                        print(f"[WARN] scan_s3_buckets: erro ao buscar tags de {bucket_name}: {exc}")
                        continue

                if tags.get("GreenOpsManaged", "").lower() != "true":
                    continue

                try:
                    # Verifica se lifecycle policy existe
                    has_lifecycle = True
                    try:
                        lc_resp = self.s3.get_bucket_lifecycle_configuration(Bucket=bucket_name)
                        rules = lc_resp.get("Rules", [])
                        # Verifica se há pelo menos uma regra habilitada
                        has_lifecycle = any(r.get("Status") == "Enabled" for r in rules)
                    except ClientError as exc:
                        if exc.response["Error"]["Code"] == "NoSuchLifecycleConfiguration":
                            has_lifecycle = False
                        else:
                            raise

                    if has_lifecycle:
                        continue

                    # Estima tamanho do bucket para calcular economia
                    bucket_size_gb = 0.0
                    try:
                        size_metric = _get_cloudwatch_average(
                            cw_client=self.cw,
                            namespace="AWS/S3",
                            metric_name="BucketSizeBytes",
                            dimensions=[
                                {"Name": "BucketName",  "Value": bucket_name},
                                {"Name": "StorageType", "Value": "StandardStorage"},
                            ],
                            days=2,
                            stat="Average",
                        )
                        if size_metric:
                            bucket_size_gb = size_metric / (1024 ** 3)
                    except Exception:
                        pass

                    est = _COST_ESTIMATES["s3_no_lifecycle"]
                    # Ajusta estimativa pelo tamanho real se disponível
                    if bucket_size_gb > 0:
                        # S3 Standard: $0.023/GB/mês; Glacier: $0.004/GB/mês
                        # Economia potencial movendo 50% dos dados para Glacier após 90 dias
                        estimated_savings = round(bucket_size_gb * 0.5 * (0.023 - 0.004), 2)
                        estimated_carbon = round(bucket_size_gb * 0.5 * 0.00002, 4)
                    else:
                        estimated_savings = est["savings"]
                        estimated_carbon = est["carbon"]

                    is_prod = _is_production_resource(tags)
                    risk = RiskLevel.LOW  # APPLY_LIFECYCLE é não-destrutivo

                    finding = _build_finding(
                        resource_type="AWS::S3::Bucket",
                        resource_id=bucket_name,
                        region=self.region,
                        waste_type=WasteType.MISCONFIGURED,
                        severity=Severity.MEDIUM,
                        description=(
                            f"Bucket S3 '{bucket_name}' sem lifecycle policy configurada. "
                            f"Objetos permanecem em Standard Storage indefinidamente."
                            + (f" Tamanho estimado: {bucket_size_gb:.1f}GB." if bucket_size_gb > 0 else "")
                        ),
                        metrics={
                            "hasLifecyclePolicy": False,
                            "estimatedSizeGb": round(bucket_size_gb, 2),
                        },
                        action=ActionType.APPLY_LIFECYCLE,
                        reason=(
                            "Sem lifecycle policy, objetos nunca transitam para classes de storage "
                            "mais baratas (Glacier, IA). Aplicar política de transição para Glacier "
                            "após 90 dias pode reduzir custos de storage em até 83%."
                        ),
                        estimated_savings=estimated_savings,
                        estimated_carbon=estimated_carbon,
                        risk_level=risk,
                        tags=tags,
                        confidence=0.9,
                    )
                    findings.append(finding)
                    print(f"[INFO] scan_s3_buckets: MISCONFIGURED finding para {bucket_name}")

                except Exception as exc:
                    print(f"[ERROR] scan_s3_buckets: erro ao processar bucket {bucket_name}: {exc}")

        except Exception as exc:
            print(f"[ERROR] scan_s3_buckets: falha geral no scan: {exc}")

        print(f"[INFO] scan_s3_buckets: {len(findings)} findings gerados")
        return findings

    # -------------------------------------------------------------------------
    # Scan: Load Balancers
    # -------------------------------------------------------------------------

    def scan_load_balancers(self) -> list[dict[str, Any]]:
        """
        Escaneia Application/Network Load Balancers idle (0 healthy hosts por 7+ dias).

        Critérios:
        - HealthyHostCount média = 0 nos últimos 7 dias em todos os target groups

        Filtros:
        - Apenas ALBs/NLBs com tag GreenOpsManaged=true

        Returns:
            Lista de findings no formato padrão.
        """
        findings: list[dict[str, Any]] = []

        try:
            print("[INFO] scan_load_balancers: iniciando")

            paginator = self.elb.get_paginator("describe_load_balancers")
            pages = paginator.paginate()

            for page in pages:
                for lb in page.get("LoadBalancers", []):
                    lb_arn = lb["LoadBalancerArn"]
                    lb_name = lb.get("LoadBalancerName", lb_arn.split("/")[-1])
                    lb_type = lb.get("Type", "application")

                    if lb.get("State", {}).get("Code") != "active":
                        continue

                    # Busca tags do LB
                    try:
                        tags_resp = self.elb.describe_tags(ResourceArns=[lb_arn])
                        tag_descs = tags_resp.get("TagDescriptions", [])
                        tags = parse_tags(tag_descs[0].get("Tags", []) if tag_descs else [])
                    except ClientError:
                        tags = {}

                    if tags.get("GreenOpsManaged", "").lower() != "true":
                        continue

                    try:
                        # Busca target groups associados ao LB
                        tg_resp = self.elb.describe_target_groups(LoadBalancerArn=lb_arn)
                        target_groups = tg_resp.get("TargetGroups", [])

                        if not target_groups:
                            # LB sem target groups = definitivamente idle
                            is_idle = True
                            avg_healthy = 0.0
                        else:
                            # Verifica healthy hosts em todos os target groups
                            healthy_counts = []
                            for tg in target_groups:
                                tg_arn = tg["TargetGroupArn"]
                                avg = _get_cloudwatch_average(
                                    cw_client=self.cw,
                                    namespace="AWS/ApplicationELB" if lb_type == "application" else "AWS/NetworkELB",
                                    metric_name="HealthyHostCount",
                                    dimensions=[
                                        {"Name": "LoadBalancer",  "Value": lb_arn.split("loadbalancer/")[-1]},
                                        {"Name": "TargetGroup",   "Value": tg_arn.split(":")[-1]},
                                    ],
                                    days=IDLE_DAYS_THRESHOLD,
                                )
                                if avg is not None:
                                    healthy_counts.append(avg)

                            avg_healthy = sum(healthy_counts) / len(healthy_counts) if healthy_counts else 0.0
                            is_idle = avg_healthy <= ALB_HEALTHY_HOSTS_THRESHOLD

                        if not is_idle:
                            continue

                        est = _COST_ESTIMATES["alb_idle"]
                        is_prod = _is_production_resource(tags)
                        risk = RiskLevel.CRITICAL if is_prod else RiskLevel.HIGH

                        finding = _build_finding(
                            resource_type="AWS::ElasticLoadBalancingV2::LoadBalancer",
                            resource_id=lb_arn,
                            region=self.region,
                            waste_type=WasteType.IDLE,
                            severity=Severity.MEDIUM,
                            description=(
                                f"Load Balancer '{lb_name}' ({lb_type}) com média de "
                                f"{avg_healthy:.1f} healthy hosts nos últimos {IDLE_DAYS_THRESHOLD} dias. "
                                f"Nenhum tráfego sendo processado."
                            ),
                            metrics={
                                "avgHealthyHostCount": avg_healthy,
                                "observationDays": IDLE_DAYS_THRESHOLD,
                                "loadBalancerType": lb_type,
                                "targetGroupCount": len(target_groups),
                            },
                            action=ActionType.DELETE,
                            reason=(
                                f"Load Balancer sem healthy hosts por {IDLE_DAYS_THRESHOLD} dias. "
                                f"ALB/NLB cobram ~$16-22/mês mesmo sem tráfego. "
                                f"Validar se pode ser removido com segurança."
                            ),
                            estimated_savings=est["savings"],
                            estimated_carbon=est["carbon"],
                            risk_level=risk,
                            tags=tags,
                            confidence=0.8,
                        )
                        findings.append(finding)
                        print(f"[INFO] scan_load_balancers: IDLE finding para {lb_name} (healthy_hosts={avg_healthy:.1f})")

                    except Exception as exc:
                        print(f"[ERROR] scan_load_balancers: erro ao processar LB {lb_name}: {exc}")

        except Exception as exc:
            print(f"[ERROR] scan_load_balancers: falha geral no scan: {exc}")

        print(f"[INFO] scan_load_balancers: {len(findings)} findings gerados")
        return findings

    # -------------------------------------------------------------------------
    # Orquestração: executa todos os scans
    # -------------------------------------------------------------------------

    def run_all_scans(self) -> list[dict[str, Any]]:
        """
        Executa todos os scans em sequência e agrega os findings.

        Cada scan é isolado — uma falha em um serviço não interrompe os demais.

        Returns:
            Lista consolidada de todos os findings gerados.
        """
        all_findings: list[dict[str, Any]] = []

        scans = [
            ("EC2 Instances",   self.scan_ec2_instances),
            ("EBS Volumes",     self.scan_ebs_volumes),
            ("Elastic IPs",     self.scan_elastic_ips),
            ("RDS Instances",   self.scan_rds_instances),
            ("Lambda Functions",self.scan_lambda_functions),
            ("S3 Buckets",      self.scan_s3_buckets),
            ("Load Balancers",  self.scan_load_balancers),
        ]

        for scan_name, scan_fn in scans:
            try:
                print(f"[INFO] run_all_scans: executando scan '{scan_name}'")
                results = scan_fn()
                all_findings.extend(results)
                print(f"[INFO] run_all_scans: '{scan_name}' concluído — {len(results)} findings")
            except Exception as exc:
                # Falha catastrófica no scan (não deveria chegar aqui pois cada scan tem try/except)
                print(f"[ERROR] run_all_scans: scan '{scan_name}' falhou inesperadamente: {exc}")

        print(f"[INFO] run_all_scans: total de {len(all_findings)} findings gerados")
        return all_findings


# =============================================================================
# Persistência e métricas
# =============================================================================

def _save_findings_to_dynamodb(
    findings: list[dict[str, Any]],
    table_name: str,
    session: boto3.Session,
) -> int:
    """
    Persiste findings no DynamoDB usando batch_writer para eficiência.

    Converte floats para Decimal antes de escrever (requisito do DynamoDB).
    Findings duplicados (mesmo findingId) são sobrescritos (upsert).

    Args:
        findings: Lista de findings no formato padrão.
        table_name: Nome da tabela DynamoDB.
        session: Sessão boto3.

    Returns:
        int: Número de findings salvos com sucesso.
    """
    if not findings:
        return 0

    dynamodb = session.resource("dynamodb")
    table = dynamodb.Table(table_name)
    saved = 0

    try:
        with table.batch_writer() as batch:
            for finding in findings:
                try:
                    item = _to_dynamodb_item(finding)
                    # Chave primária: resourceId (hash) + findingId (range)
                    batch.put_item(Item=item)
                    saved += 1
                except Exception as exc:
                    print(f"[ERROR] _save_findings_to_dynamodb: erro ao salvar finding {finding.get('findingId')}: {exc}")

        print(f"[INFO] _save_findings_to_dynamodb: {saved}/{len(findings)} findings salvos em '{table_name}'")

    except ClientError as exc:
        print(f"[ERROR] _save_findings_to_dynamodb: erro de acesso ao DynamoDB: {exc}")

    return saved


def _publish_cloudwatch_metrics(
    findings: list[dict[str, Any]],
    region: str,
    session: boto3.Session,
) -> None:
    """
    Publica métricas customizadas no CloudWatch namespace 'GreenOps/Discovery'.

    Métricas publicadas:
    - FindingsCount (total)
    - FindingsCount por WasteType (IDLE, ORPHAN, OVERSIZED, MISCONFIGURED)
    - FindingsCount por Severity (LOW, MEDIUM, HIGH, CRITICAL)
    - TotalEstimatedMonthlySavings (USD)
    - TotalEstimatedMonthlyCarbonReduction (MTCO2e)

    Args:
        findings: Lista de findings gerados.
        region: Região AWS.
        session: Sessão boto3.
    """
    if not findings:
        return

    try:
        cw = session.client("cloudwatch", region_name=region)
        timestamp = _now_utc()

        # Agrega contagens por tipo e severidade
        by_waste: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        total_savings = 0.0
        total_carbon = 0.0

        for f in findings:
            waste = f.get("wasteType", "UNKNOWN")
            severity = f.get("severity", "UNKNOWN")
            by_waste[waste] = by_waste.get(waste, 0) + 1
            by_severity[severity] = by_severity.get(severity, 0) + 1

            rec = f.get("recommendation", {})
            total_savings += float(rec.get("estimatedMonthlySavings", 0))
            total_carbon += float(rec.get("estimatedMonthlyCarbonReduction", 0))

        metric_data = [
            # Total de findings
            {
                "MetricName": "FindingsCount",
                "Dimensions": [{"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "dev")}],
                "Value": len(findings),
                "Unit": "Count",
                "Timestamp": timestamp,
            },
            # Economia total estimada
            {
                "MetricName": "TotalEstimatedMonthlySavings",
                "Dimensions": [{"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "dev")}],
                "Value": round(total_savings, 2),
                "Unit": "None",
                "Timestamp": timestamp,
            },
            # Carbono total estimado
            {
                "MetricName": "TotalEstimatedMonthlyCarbonReduction",
                "Dimensions": [{"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "dev")}],
                "Value": round(total_carbon, 4),
                "Unit": "None",
                "Timestamp": timestamp,
            },
        ]

        # Métricas por WasteType
        for waste_type, count in by_waste.items():
            metric_data.append({
                "MetricName": "FindingsCount",
                "Dimensions": [
                    {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "dev")},
                    {"Name": "WasteType",   "Value": waste_type},
                ],
                "Value": count,
                "Unit": "Count",
                "Timestamp": timestamp,
            })

        # Métricas por Severity
        for severity, count in by_severity.items():
            metric_data.append({
                "MetricName": "FindingsCount",
                "Dimensions": [
                    {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "dev")},
                    {"Name": "Severity",    "Value": severity},
                ],
                "Value": count,
                "Unit": "Count",
                "Timestamp": timestamp,
            })

        # CloudWatch aceita no máximo 20 métricas por chamada
        chunk_size = 20
        for i in range(0, len(metric_data), chunk_size):
            chunk = metric_data[i:i + chunk_size]
            cw.put_metric_data(Namespace="GreenOps/Discovery", MetricData=chunk)

        print(f"[INFO] _publish_cloudwatch_metrics: {len(metric_data)} métricas publicadas")

    except Exception as exc:
        # Falha em métricas não deve interromper o handler
        print(f"[ERROR] _publish_cloudwatch_metrics: falha ao publicar métricas: {exc}")


# =============================================================================
# Lambda entry point
# =============================================================================

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Entry point da função Lambda de Discovery.

    Fluxo de execução:
    1. Instancia DiscoveryOrchestrator
    2. Executa todos os scans (EC2, EBS, EIP, RDS, Lambda, S3, ALB)
    3. Salva findings no DynamoDB (FINDINGS_TABLE)
    4. Publica métricas no CloudWatch (GreenOps/Discovery)
    5. Retorna resposta HTTP 200 com contagem de findings

    Variáveis de ambiente esperadas:
    - FINDINGS_TABLE: Nome da tabela DynamoDB (obrigatório)
    - AWS_REGION: Região AWS (padrão: us-east-1)
    - ENVIRONMENT: Ambiente de execução (padrão: dev)

    Args:
        event: Evento Lambda (EventBridge schedule ou invocação manual).
            Campos opcionais:
            - ``action``: "full-discovery" (padrão) ou "dry-run"
            - ``environment``: sobrescreve ENVIRONMENT env var
        context: Contexto Lambda (LambdaContext).

    Returns:
        dict: Resposta HTTP-like com statusCode 200 e body JSON contendo:
            - findingsCount: total de findings gerados
            - savedCount: total de findings salvos no DynamoDB
            - byWasteType: contagem por tipo de desperdício
            - bySeverity: contagem por severidade
            - totalEstimatedMonthlySavings: economia total estimada (USD)
            - region: região escaneada
            - timestamp: timestamp da execução
    """
    print(f"[INFO] lambda_handler: iniciando discovery — event={json.dumps(event)}")

    region = os.environ.get("AWS_REGION", "us-east-1")
    findings_table = os.environ.get("FINDINGS_TABLE", "")
    action = event.get("action", "full-discovery")

    if not findings_table:
        print("[WARN] lambda_handler: FINDINGS_TABLE não configurada — findings não serão persistidos")

    # Instancia o orquestrador
    orchestrator = DiscoveryOrchestrator(region=region)
    session = orchestrator._session

    # Executa todos os scans
    all_findings: list[dict[str, Any]] = []
    try:
        all_findings = orchestrator.run_all_scans()
    except Exception as exc:
        print(f"[ERROR] lambda_handler: run_all_scans falhou: {exc}")
        # Retorna erro mas não lança exceção — permite retry pelo EventBridge
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": str(exc),
                "findingsCount": 0,
                "timestamp": _iso_now(),
            }),
        }

    # Persiste no DynamoDB
    saved_count = 0
    if findings_table and action != "dry-run":
        saved_count = _save_findings_to_dynamodb(all_findings, findings_table, session)
    elif action == "dry-run":
        print(f"[INFO] lambda_handler: dry-run mode — {len(all_findings)} findings NÃO salvos")
        saved_count = 0
    else:
        print("[WARN] lambda_handler: FINDINGS_TABLE vazia — pulando persistência")

    # Publica métricas no CloudWatch
    _publish_cloudwatch_metrics(all_findings, region, session)

    # Agrega estatísticas para a resposta
    by_waste: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    total_savings = 0.0

    for f in all_findings:
        waste = f.get("wasteType", "UNKNOWN")
        severity = f.get("severity", "UNKNOWN")
        by_waste[waste] = by_waste.get(waste, 0) + 1
        by_severity[severity] = by_severity.get(severity, 0) + 1
        total_savings += float(f.get("recommendation", {}).get("estimatedMonthlySavings", 0))

    response_body = {
        "findingsCount": len(all_findings),
        "savedCount": saved_count,
        "byWasteType": by_waste,
        "bySeverity": by_severity,
        "totalEstimatedMonthlySavings": round(total_savings, 2),
        "region": region,
        "timestamp": _iso_now(),
        "dryRun": action == "dry-run",
    }

    print(f"[INFO] lambda_handler: concluído — {len(all_findings)} findings, ${total_savings:.2f}/mês estimado")

    return {
        "statusCode": 200,
        "body": json.dumps(response_body),
    }
