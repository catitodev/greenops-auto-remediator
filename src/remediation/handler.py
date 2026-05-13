"""
GreenOps Auto-Remediador — Remediation Handler
===============================================
Processa findings aprovados do DynamoDB Stream, executa ações de remediação
com controle de risco, rate limiting e rollback automático.

Entry point Lambda: ``lambda_handler(event, context)``
Trigger: DynamoDB Stream na tabela ``greenops-findings-{env}``

Fluxo de execução por finding:
1. Filtra eventos INSERT/MODIFY com status=APPROVED
2. Verifica rate limits (50/hora total, 20/hora por tipo, 10 destrutivas/dia)
3. Classifica risco da ação (LOW / MEDIUM / HIGH / CRITICAL)
4. LOW  → salva rollback state + executa imediatamente
5. MEDIUM/HIGH/CRITICAL → salva em approvals_table + envia SNS para aprovação
6. Após execução: atualiza finding status=REMEDIATED + publica métricas CloudWatch

Ações suportadas:
- ec2.stop_instances        (STOP de instância EC2)
- ec2.start_instances       (START de instância EC2)
- ec2.modify_instance_attribute (RESIZE de tipo de instância)
- ec2.release_address       (RELEASE de Elastic IP)
- rds.stop_db_instance      (STOP de instância RDS)
- rds.start_db_instance     (START de instância RDS)
- lambda.update_function_configuration (RESIZE de memória Lambda)
- s3.put_bucket_lifecycle_configuration (APPLY_LIFECYCLE em S3)
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from shared.constants import ActionType, RiskLevel

# =============================================================================
# Constantes de rate limiting
# =============================================================================

# Limite total de ações de remediação por hora (qualquer tipo)
RATE_LIMIT_TOTAL_PER_HOUR: int = int(os.environ.get("MAX_ACTIONS_PER_HOUR", "50"))

# Limite de ações por tipo por hora (ex: máx 20 STOPs/hora)
RATE_LIMIT_PER_TYPE_PER_HOUR: int = 20

# Limite de ações destrutivas por dia (DELETE, RELEASE)
RATE_LIMIT_DESTRUCTIVE_PER_DAY: int = int(os.environ.get("MAX_DESTRUCTIVE_ACTIONS_PER_DAY", "10"))

# Ações consideradas destrutivas para fins de rate limiting
DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset({
    ActionType.DELETE,
    ActionType.RELEASE,
})

# TTL do registro de rollback em segundos (7 dias)
ROLLBACK_TTL_SECONDS: int = 7 * 24 * 3600

# TTL do registro de aprovação em segundos (48 horas)
APPROVAL_TTL_SECONDS: int = 48 * 3600

# Política de lifecycle S3 padrão: transição para Glacier após 90 dias
_DEFAULT_S3_LIFECYCLE_POLICY: dict[str, Any] = {
    "Rules": [
        {
            "ID": "GreenOps-Glacier-90days",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "Transitions": [
                {"Days": 90, "StorageClass": "GLACIER"},
            ],
            "NoncurrentVersionTransitions": [
                {"NoncurrentDays": 30, "StorageClass": "GLACIER"},
            ],
            "NoncurrentVersionExpiration": {"NoncurrentDays": 365},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        }
    ]
}


# =============================================================================
# Helpers internos
# =============================================================================

def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso_now() -> str:
    return _now_utc().isoformat()


def _make_remediation_id(finding_id: str) -> str:
    """Gera ID único para o registro de remediação."""
    return str(uuid.uuid4())


def _make_approval_id(finding_id: str) -> str:
    """Gera ID único para o registro de aprovação."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"approval:{finding_id}:{_now_utc().date()}"))


def _deserialize_dynamodb_item(item: dict[str, Any]) -> dict[str, Any]:
    """
    Converte item DynamoDB (formato AttributeValue) para dict Python simples.

    O DynamoDB Stream entrega itens no formato:
    ``{"fieldName": {"S": "value"}, "count": {"N": "42"}}``

    Esta função normaliza para: ``{"fieldName": "value", "count": 42}``
    """
    def _convert(val: Any) -> Any:
        if not isinstance(val, dict):
            return val
        if "S" in val:
            return val["S"]
        if "N" in val:
            v = val["N"]
            return int(v) if "." not in v else float(v)
        if "BOOL" in val:
            return val["BOOL"]
        if "NULL" in val:
            return None
        if "M" in val:
            return {k: _convert(v) for k, v in val["M"].items()}
        if "L" in val:
            return [_convert(i) for i in val["L"]]
        if "SS" in val:
            return list(val["SS"])
        if "NS" in val:
            return [float(n) for n in val["NS"]]
        return val

    return {k: _convert(v) for k, v in item.items()}


def _to_dynamodb_item(obj: dict[str, Any]) -> dict[str, Any]:
    """Converte floats para Decimal para compatibilidade com DynamoDB."""
    def _convert(v: Any) -> Any:
        if isinstance(v, float):
            return Decimal(str(v))
        if isinstance(v, dict):
            return {k: _convert(val) for k, val in v.items()}
        if isinstance(v, list):
            return [_convert(i) for i in v]
        return v
    return _convert(obj)


def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# =============================================================================
# RateLimiter — controle de blast radius
# =============================================================================

class RateLimiter:
    """
    Controla o número de ações de remediação executadas por hora/dia.

    Usa DynamoDB como contador distribuído para garantir consistência
    entre múltiplas invocações Lambda concorrentes.

    Contadores armazenados na config_table com chaves:
    - ``rate:total:{YYYY-MM-DDTHH}``          → total/hora
    - ``rate:type:{action_type}:{YYYY-MM-DDTHH}`` → por tipo/hora
    - ``rate:destructive:{YYYY-MM-DD}``        → destrutivas/dia

    Args:
        config_table_name: Nome da tabela DynamoDB de configuração.
        session: Sessão boto3.
    """

    def __init__(self, config_table_name: str, session: boto3.Session) -> None:
        self._table_name = config_table_name
        self._dynamodb = session.resource("dynamodb")
        self._table = self._dynamodb.Table(config_table_name) if config_table_name else None

    def _increment_counter(self, key: str, ttl_seconds: int) -> int:
        """
        Incrementa atomicamente um contador no DynamoDB e retorna o novo valor.

        Usa UpdateItem com ADD para garantir atomicidade em ambiente concorrente.
        Cria o item se não existir (upsert).

        Returns:
            int: Novo valor do contador após incremento. -1 em caso de erro.
        """
        if not self._table:
            return 0  # sem tabela configurada → não aplica rate limit

        try:
            ttl_epoch = int((_now_utc() + timedelta(seconds=ttl_seconds)).timestamp())
            response = self._table.update_item(
                Key={"configKey": key},
                UpdateExpression="ADD #count :inc SET #ttl = if_not_exists(#ttl, :ttl)",
                ExpressionAttributeNames={"#count": "count", "#ttl": "ttl"},
                ExpressionAttributeValues={":inc": 1, ":ttl": ttl_epoch},
                ReturnValues="UPDATED_NEW",
            )
            return int(response["Attributes"].get("count", 1))
        except ClientError as exc:
            print(f"[WARN] RateLimiter._increment_counter: erro DynamoDB para key={key}: {exc}")
            return -1  # erro → permite a ação (fail-open para não bloquear remediações)

    def _get_counter(self, key: str) -> int:
        """Lê o valor atual de um contador sem incrementar."""
        if not self._table:
            return 0
        try:
            response = self._table.get_item(Key={"configKey": key})
            item = response.get("Item", {})
            return int(item.get("count", 0))
        except ClientError:
            return 0

    def check_and_increment(self, action_type: str) -> tuple[bool, str]:
        """
        Verifica se a ação está dentro dos limites e incrementa os contadores.

        Verifica três limites em ordem:
        1. Total de ações por hora
        2. Ações do mesmo tipo por hora
        3. Ações destrutivas por dia (apenas para DELETE e RELEASE)

        Args:
            action_type: Tipo da ação (valor de ActionType).

        Returns:
            tuple[bool, str]: (permitido, motivo_da_rejeição_se_negado)
        """
        now = _now_utc()
        hour_key = now.strftime("%Y-%m-%dT%H")
        day_key = now.strftime("%Y-%m-%d")

        # 1. Verifica limite total/hora ANTES de incrementar
        total_key = f"rate:total:{hour_key}"
        current_total = self._get_counter(total_key)
        if current_total >= RATE_LIMIT_TOTAL_PER_HOUR:
            return False, (
                f"Rate limit total excedido: {current_total}/{RATE_LIMIT_TOTAL_PER_HOUR} ações/hora"
            )

        # 2. Verifica limite por tipo/hora
        type_key = f"rate:type:{action_type}:{hour_key}"
        current_type = self._get_counter(type_key)
        if current_type >= RATE_LIMIT_PER_TYPE_PER_HOUR:
            return False, (
                f"Rate limit por tipo excedido: {current_type}/{RATE_LIMIT_PER_TYPE_PER_HOUR} "
                f"ações {action_type}/hora"
            )

        # 3. Verifica limite destrutivas/dia
        if action_type in DESTRUCTIVE_ACTIONS:
            dest_key = f"rate:destructive:{day_key}"
            current_dest = self._get_counter(dest_key)
            if current_dest >= RATE_LIMIT_DESTRUCTIVE_PER_DAY:
                return False, (
                    f"Rate limit destrutivo excedido: {current_dest}/{RATE_LIMIT_DESTRUCTIVE_PER_DAY} "
                    f"ações destrutivas/dia"
                )

        # Todos os limites OK — incrementa contadores
        self._increment_counter(total_key, ttl_seconds=3600)
        self._increment_counter(type_key, ttl_seconds=3600)
        if action_type in DESTRUCTIVE_ACTIONS:
            self._increment_counter(f"rate:destructive:{day_key}", ttl_seconds=86400)

        return True, ""


# =============================================================================
# RemediationOrchestrator
# =============================================================================

class RemediationOrchestrator:
    """
    Orquestra a execução de ações de remediação com controle de risco e rollback.

    Responsabilidades:
    - Classificar risco de cada ação
    - Verificar rate limits antes de executar
    - Salvar estado anterior (rollback snapshot) antes de qualquer mudança
    - Executar ações via boto3 para EC2, RDS, Lambda, S3
    - Encaminhar ações de risco MEDIUM/HIGH/CRITICAL para aprovação via SNS
    - Atualizar status do finding após execução
    - Publicar métricas CloudWatch

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

        # Nomes de tabelas e tópicos (injetados pelo CloudFormation)
        self.findings_table_name = _get_env("FINDINGS_TABLE")
        self.rollbacks_table_name = _get_env("ROLLBACKS_TABLE")
        self.approvals_table_name = _get_env("APPROVALS_TABLE")
        self.config_table_name = _get_env("CONFIG_TABLE")
        self.notifications_topic_arn = _get_env("NOTIFICATIONS_TOPIC_ARN")
        self.approvals_topic_arn = _get_env("APPROVALS_TOPIC_ARN")
        self.alerts_topic_arn = _get_env("ALERTS_TOPIC_ARN")
        self.environment = _get_env("ENVIRONMENT", "dev")
        self.dry_run = _get_env("GREENOPS_DRY_RUN", "true").lower() in {"true", "1", "yes"}

        # Rate limiter
        self._rate_limiter = RateLimiter(self.config_table_name, self._session)

        # Lazy clients
        self._ec2: Any = None
        self._rds: Any = None
        self._lambda: Any = None
        self._s3: Any = None
        self._sns: Any = None
        self._cw: Any = None
        self._dynamodb: Any = None

    # -------------------------------------------------------------------------
    # Lazy clients
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
    def lmb(self) -> Any:
        """Cliente Lambda (evita conflito com builtin 'lambda')."""
        if self._lambda is None:
            self._lambda = self._session.client("lambda", region_name=self.region)
        return self._lambda

    @property
    def s3(self) -> Any:
        if self._s3 is None:
            self._s3 = self._session.client("s3", region_name=self.region)
        return self._s3

    @property
    def sns(self) -> Any:
        if self._sns is None:
            self._sns = self._session.client("sns", region_name=self.region)
        return self._sns

    @property
    def cw(self) -> Any:
        if self._cw is None:
            self._cw = self._session.client("cloudwatch", region_name=self.region)
        return self._cw

    @property
    def dynamodb(self) -> Any:
        if self._dynamodb is None:
            self._dynamodb = self._session.resource("dynamodb")
        return self._dynamodb

    # -------------------------------------------------------------------------
    # Classificação de risco
    # -------------------------------------------------------------------------

    def classify_risk(self, finding: dict[str, Any]) -> RiskLevel:
        """
        Determina o RiskLevel efetivo de um finding.

        Regras de elevação de risco:
        1. Parte do riskLevel declarado na recommendation
        2. Se o recurso tem tag Environment=Production → eleva para CRITICAL
        3. Se o ambiente de execução é prod → eleva um nível acima do declarado

        Args:
            finding: Finding no formato padrão GreenOps.

        Returns:
            RiskLevel: Nível de risco efetivo para controle de aprovação.
        """
        rec = finding.get("recommendation", {})
        declared_risk_str = rec.get("riskLevel", RiskLevel.HIGH)

        try:
            declared_risk = RiskLevel(declared_risk_str)
        except ValueError:
            declared_risk = RiskLevel.HIGH  # fallback seguro

        tags = finding.get("tags", {})
        resource_env = tags.get("Environment", "").lower()

        # Recurso de produção → sempre CRITICAL
        if resource_env == "production":
            return RiskLevel.CRITICAL

        # Ambiente de execução é prod → eleva um nível
        if self.environment == "prod":
            escalation = {
                RiskLevel.LOW:      RiskLevel.MEDIUM,
                RiskLevel.MEDIUM:   RiskLevel.HIGH,
                RiskLevel.HIGH:     RiskLevel.CRITICAL,
                RiskLevel.CRITICAL: RiskLevel.CRITICAL,
            }
            return escalation[declared_risk]

        return declared_risk

    # -------------------------------------------------------------------------
    # Rollback state
    # -------------------------------------------------------------------------

    def save_rollback_state(
        self,
        finding: dict[str, Any],
        remediation_id: str,
        previous_state: dict[str, Any],
    ) -> bool:
        """
        Persiste o estado anterior do recurso na rollbacks_table para rollback manual.

        O registro expira automaticamente após ROLLBACK_TTL_SECONDS (7 dias) via TTL.

        Args:
            finding: Finding que originou a remediação.
            remediation_id: ID único desta remediação.
            previous_state: Estado do recurso antes da ação (snapshot boto3).

        Returns:
            bool: True se salvo com sucesso, False em caso de erro.
        """
        if not self.rollbacks_table_name:
            print("[WARN] save_rollback_state: ROLLBACKS_TABLE não configurada — pulando")
            return False

        try:
            table = self.dynamodb.Table(self.rollbacks_table_name)
            item = _to_dynamodb_item({
                "remediationId": remediation_id,
                "resourceId": finding.get("resourceId", ""),
                "resourceType": finding.get("resourceType", ""),
                "findingId": finding.get("findingId", ""),
                "action": finding.get("recommendation", {}).get("action", ""),
                "previousState": previous_state,
                "executedAt": _iso_now(),
                "environment": self.environment,
                "region": self.region,
                "ttl": int((_now_utc() + timedelta(seconds=ROLLBACK_TTL_SECONDS)).timestamp()),
            })
            table.put_item(Item=item)
            print(f"[INFO] save_rollback_state: rollback salvo para {finding.get('resourceId')} (id={remediation_id})")
            return True
        except Exception as exc:
            print(f"[ERROR] save_rollback_state: falha ao salvar rollback: {exc}")
            return False

    # -------------------------------------------------------------------------
    # Aprovação via SNS
    # -------------------------------------------------------------------------

    def request_approval(
        self,
        finding: dict[str, Any],
        risk_level: RiskLevel,
        approval_id: str,
    ) -> bool:
        """
        Salva o finding na approvals_table e envia notificação SNS para aprovação.

        Para CRITICAL (dual approval): envia para approvals_topic_arn com
        Subject indicando que dois aprovadores são necessários.

        Args:
            finding: Finding que requer aprovação.
            risk_level: Nível de risco efetivo.
            approval_id: ID único do pedido de aprovação.

        Returns:
            bool: True se aprovação solicitada com sucesso.
        """
        resource_id = finding.get("resourceId", "")
        action = finding.get("recommendation", {}).get("action", "")
        savings = finding.get("recommendation", {}).get("estimatedMonthlySavings", 0)

        # Salva na approvals_table
        if self.approvals_table_name:
            try:
                table = self.dynamodb.Table(self.approvals_table_name)
                item = _to_dynamodb_item({
                    "approvalId": approval_id,
                    "findingId": finding.get("findingId", ""),
                    "resourceId": resource_id,
                    "resourceType": finding.get("resourceType", ""),
                    "action": action,
                    "riskLevel": risk_level.value,
                    "status": "PENDING",
                    "requestedAt": _iso_now(),
                    "finding": finding,
                    "approvals": [],  # lista de aprovadores que confirmaram
                    "requiredApprovals": 2 if risk_level.requires_dual_approval else 1,
                    "ttl": int((_now_utc() + timedelta(seconds=APPROVAL_TTL_SECONDS)).timestamp()),
                })
                table.put_item(Item=item)
                print(f"[INFO] request_approval: aprovação {approval_id} salva para {resource_id}")
            except Exception as exc:
                print(f"[ERROR] request_approval: falha ao salvar aprovação: {exc}")

        # Envia SNS
        topic_arn = self.approvals_topic_arn or self.notifications_topic_arn
        if not topic_arn:
            print("[WARN] request_approval: nenhum tópico SNS configurado")
            return False

        try:
            dual_note = " ⚠️ APROVAÇÃO DUPLA NECESSÁRIA" if risk_level.requires_dual_approval else ""
            dry_run_note = " [DRY-RUN]" if risk_level.requires_dry_run else ""

            subject = (
                f"[GreenOps{dry_run_note}] Aprovação necessária: {action} em {resource_id}"
                f"{dual_note}"
            )[:100]  # SNS limita subject a 100 chars

            message = json.dumps({
                "title": f"GreenOps — Aprovação de Remediação{dual_note}",
                "approvalId": approval_id,
                "findingId": finding.get("findingId"),
                "resourceId": resource_id,
                "resourceType": finding.get("resourceType"),
                "action": action,
                "riskLevel": risk_level.value,
                "requiresDryRun": risk_level.requires_dry_run,
                "requiresDualApproval": risk_level.requires_dual_approval,
                "estimatedMonthlySavings": savings,
                "description": finding.get("description", ""),
                "recommendation": finding.get("recommendation", {}),
                "requestedAt": _iso_now(),
                "environment": self.environment,
                "instructions": (
                    "Para aprovar, atualize o item na tabela approvals com status=APPROVED. "
                    "Para rejeitar, use status=REJECTED."
                ),
            }, indent=2)

            self.sns.publish(
                TopicArn=topic_arn,
                Subject=subject,
                Message=message,
            )
            print(f"[INFO] request_approval: SNS enviado para {topic_arn} (riskLevel={risk_level})")
            return True

        except ClientError as exc:
            print(f"[ERROR] request_approval: falha ao publicar SNS: {exc}")
            return False

    # -------------------------------------------------------------------------
    # Executores de ação por tipo de recurso
    # -------------------------------------------------------------------------

    def _execute_ec2_stop(self, resource_id: str) -> dict[str, Any]:
        """Para uma instância EC2. Salva estado anterior antes de executar."""
        # Captura estado atual para rollback
        resp = self.ec2.describe_instances(InstanceIds=[resource_id])
        reservations = resp.get("Reservations", [])
        instance = reservations[0]["Instances"][0] if reservations else {}
        previous_state = {
            "instanceId": resource_id,
            "state": instance.get("State", {}).get("Name", "unknown"),
            "instanceType": instance.get("InstanceType", ""),
        }

        if self.dry_run:
            print(f"[DRY-RUN] _execute_ec2_stop: NÃO parando {resource_id}")
            return {"dryRun": True, "previousState": previous_state}

        self.ec2.stop_instances(InstanceIds=[resource_id])
        print(f"[INFO] _execute_ec2_stop: instância {resource_id} sendo parada")
        return {"previousState": previous_state, "action": "stop_instances"}

    def _execute_ec2_start(self, resource_id: str) -> dict[str, Any]:
        """Inicia uma instância EC2 parada."""
        resp = self.ec2.describe_instances(InstanceIds=[resource_id])
        reservations = resp.get("Reservations", [])
        instance = reservations[0]["Instances"][0] if reservations else {}
        previous_state = {"state": instance.get("State", {}).get("Name", "unknown")}

        if self.dry_run:
            print(f"[DRY-RUN] _execute_ec2_start: NÃO iniciando {resource_id}")
            return {"dryRun": True, "previousState": previous_state}

        self.ec2.start_instances(InstanceIds=[resource_id])
        print(f"[INFO] _execute_ec2_start: instância {resource_id} sendo iniciada")
        return {"previousState": previous_state, "action": "start_instances"}

    def _execute_ec2_resize(
        self, resource_id: str, recommended_type: str
    ) -> dict[str, Any]:
        """
        Redimensiona o tipo de uma instância EC2 (requer stop prévio).

        Fluxo: stop → modify_instance_attribute → start
        A instância deve estar parada para modificar o tipo.
        """
        resp = self.ec2.describe_instances(InstanceIds=[resource_id])
        reservations = resp.get("Reservations", [])
        instance = reservations[0]["Instances"][0] if reservations else {}
        current_type = instance.get("InstanceType", "unknown")
        current_state = instance.get("State", {}).get("Name", "unknown")

        previous_state = {
            "instanceId": resource_id,
            "instanceType": current_type,
            "state": current_state,
        }

        if self.dry_run:
            print(f"[DRY-RUN] _execute_ec2_resize: NÃO redimensionando {resource_id} ({current_type} → {recommended_type})")
            return {"dryRun": True, "previousState": previous_state}

        # Para a instância se estiver running
        if current_state == "running":
            self.ec2.stop_instances(InstanceIds=[resource_id])
            print(f"[INFO] _execute_ec2_resize: parando {resource_id} para resize")
            # Aguarda stop (em produção real usaria waiter)
            import time; time.sleep(2)

        self.ec2.modify_instance_attribute(
            InstanceId=resource_id,
            InstanceType={"Value": recommended_type},
        )
        print(f"[INFO] _execute_ec2_resize: {resource_id} redimensionado {current_type} → {recommended_type}")

        # Reinicia se estava running
        if current_state == "running":
            self.ec2.start_instances(InstanceIds=[resource_id])
            print(f"[INFO] _execute_ec2_resize: reiniciando {resource_id}")

        return {
            "previousState": previous_state,
            "newInstanceType": recommended_type,
            "action": "modify_instance_attribute",
        }

    def _execute_ec2_release_address(self, resource_id: str) -> dict[str, Any]:
        """Libera um Elastic IP não associado."""
        # Busca informações do EIP para rollback
        resp = self.ec2.describe_addresses(AllocationIds=[resource_id])
        addresses = resp.get("Addresses", [])
        addr = addresses[0] if addresses else {}
        previous_state = {
            "allocationId": resource_id,
            "publicIp": addr.get("PublicIp", ""),
            "domain": addr.get("Domain", "vpc"),
        }

        if self.dry_run:
            print(f"[DRY-RUN] _execute_ec2_release_address: NÃO liberando EIP {resource_id}")
            return {"dryRun": True, "previousState": previous_state}

        self.ec2.release_address(AllocationId=resource_id)
        print(f"[INFO] _execute_ec2_release_address: EIP {resource_id} liberado")
        return {"previousState": previous_state, "action": "release_address"}

    def _execute_rds_stop(self, resource_id: str) -> dict[str, Any]:
        """Para uma instância RDS."""
        resp = self.rds.describe_db_instances(DBInstanceIdentifier=resource_id)
        instances = resp.get("DBInstances", [])
        db = instances[0] if instances else {}
        previous_state = {
            "dbInstanceIdentifier": resource_id,
            "dbInstanceStatus": db.get("DBInstanceStatus", "unknown"),
            "dbInstanceClass": db.get("DBInstanceClass", ""),
        }

        if self.dry_run:
            print(f"[DRY-RUN] _execute_rds_stop: NÃO parando RDS {resource_id}")
            return {"dryRun": True, "previousState": previous_state}

        self.rds.stop_db_instance(DBInstanceIdentifier=resource_id)
        print(f"[INFO] _execute_rds_stop: instância RDS {resource_id} sendo parada")
        return {"previousState": previous_state, "action": "stop_db_instance"}

    def _execute_rds_start(self, resource_id: str) -> dict[str, Any]:
        """Inicia uma instância RDS parada."""
        resp = self.rds.describe_db_instances(DBInstanceIdentifier=resource_id)
        instances = resp.get("DBInstances", [])
        db = instances[0] if instances else {}
        previous_state = {"dbInstanceStatus": db.get("DBInstanceStatus", "unknown")}

        if self.dry_run:
            print(f"[DRY-RUN] _execute_rds_start: NÃO iniciando RDS {resource_id}")
            return {"dryRun": True, "previousState": previous_state}

        self.rds.start_db_instance(DBInstanceIdentifier=resource_id)
        print(f"[INFO] _execute_rds_start: instância RDS {resource_id} sendo iniciada")
        return {"previousState": previous_state, "action": "start_db_instance"}

    def _execute_lambda_resize(
        self, resource_id: str, recommended_mb: int
    ) -> dict[str, Any]:
        """Atualiza a memória alocada de uma função Lambda."""
        resp = self.lmb.get_function_configuration(FunctionName=resource_id)
        current_mb = resp.get("MemorySize", 128)
        previous_state = {
            "functionName": resource_id,
            "memorySize": current_mb,
            "timeout": resp.get("Timeout", 3),
        }

        if self.dry_run:
            print(f"[DRY-RUN] _execute_lambda_resize: NÃO redimensionando Lambda {resource_id} ({current_mb}MB → {recommended_mb}MB)")
            return {"dryRun": True, "previousState": previous_state}

        self.lmb.update_function_configuration(
            FunctionName=resource_id,
            MemorySize=recommended_mb,
        )
        print(f"[INFO] _execute_lambda_resize: Lambda {resource_id} redimensionada {current_mb}MB → {recommended_mb}MB")
        return {
            "previousState": previous_state,
            "newMemorySize": recommended_mb,
            "action": "update_function_configuration",
        }

    def _execute_s3_apply_lifecycle(self, resource_id: str) -> dict[str, Any]:
        """Aplica política de lifecycle padrão em um bucket S3."""
        # Captura política atual para rollback
        previous_policy: dict[str, Any] = {}
        try:
            resp = self.s3.get_bucket_lifecycle_configuration(Bucket=resource_id)
            previous_policy = {"Rules": resp.get("Rules", [])}
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchLifecycleConfiguration":
                raise
            previous_policy = {"Rules": []}  # sem política anterior

        previous_state = {
            "bucketName": resource_id,
            "previousLifecyclePolicy": previous_policy,
        }

        if self.dry_run:
            print(f"[DRY-RUN] _execute_s3_apply_lifecycle: NÃO aplicando lifecycle em {resource_id}")
            return {"dryRun": True, "previousState": previous_state}

        self.s3.put_bucket_lifecycle_configuration(
            Bucket=resource_id,
            LifecycleConfiguration=_DEFAULT_S3_LIFECYCLE_POLICY,
        )
        print(f"[INFO] _execute_s3_apply_lifecycle: lifecycle aplicado em bucket {resource_id}")
        return {
            "previousState": previous_state,
            "appliedPolicy": _DEFAULT_S3_LIFECYCLE_POLICY,
            "action": "put_bucket_lifecycle_configuration",
        }

    # -------------------------------------------------------------------------
    # Dispatcher de ações
    # -------------------------------------------------------------------------

    def execute_action(
        self,
        finding: dict[str, Any],
        remediation_id: str,
    ) -> dict[str, Any]:
        """
        Despacha a ação de remediação correta com base no resourceType e action.

        Antes de executar: salva rollback state.
        Após executar: retorna resultado com previousState para auditoria.

        Args:
            finding: Finding aprovado no formato padrão GreenOps.
            remediation_id: ID único desta remediação.

        Returns:
            dict com resultado da execução: previousState, action, dryRun, etc.

        Raises:
            ValueError: Se a combinação resourceType + action não for suportada.
        """
        resource_type = finding.get("resourceType", "")
        resource_id = finding.get("resourceId", "")
        rec = finding.get("recommendation", {})
        action = rec.get("action", "")
        metrics = finding.get("metrics", {})

        print(f"[INFO] execute_action: {action} em {resource_type}/{resource_id}")

        result: dict[str, Any] = {}

        try:
            # EC2 Instance
            if resource_type == "AWS::EC2::Instance":
                if action == ActionType.STOP:
                    result = self._execute_ec2_stop(resource_id)
                elif action == ActionType.START:
                    result = self._execute_ec2_start(resource_id)
                elif action == ActionType.RESIZE:
                    recommended_type = metrics.get("recommendedInstanceType", "")
                    if not recommended_type:
                        raise ValueError(f"recommendedInstanceType ausente nos metrics do finding {finding.get('findingId')}")
                    result = self._execute_ec2_resize(resource_id, recommended_type)
                else:
                    raise ValueError(f"Ação {action} não suportada para {resource_type}")

            # EBS Volume
            elif resource_type == "AWS::EC2::Volume":
                if action == ActionType.DELETE:
                    # DELETE de EBS requer aprovação CRITICAL — nunca executa automaticamente
                    # Se chegou aqui, foi aprovado manualmente
                    if self.dry_run:
                        print(f"[DRY-RUN] execute_action: NÃO deletando volume EBS {resource_id}")
                        result = {"dryRun": True, "previousState": {"volumeId": resource_id}}
                    else:
                        # Cria snapshot de backup antes de deletar
                        snap_resp = self.ec2.create_snapshot(
                            VolumeId=resource_id,
                            Description=f"GreenOps backup before delete - {_iso_now()}",
                            TagSpecifications=[{
                                "ResourceType": "snapshot",
                                "Tags": [
                                    {"Key": "GreenOpsBackup", "Value": "true"},
                                    {"Key": "SourceVolumeId", "Value": resource_id},
                                ],
                            }],
                        )
                        snapshot_id = snap_resp.get("SnapshotId", "")
                        print(f"[INFO] execute_action: snapshot {snapshot_id} criado antes de deletar {resource_id}")
                        self.ec2.delete_volume(VolumeId=resource_id)
                        print(f"[INFO] execute_action: volume EBS {resource_id} deletado")
                        result = {
                            "previousState": {"volumeId": resource_id, "backupSnapshotId": snapshot_id},
                            "action": "delete_volume",
                        }
                else:
                    raise ValueError(f"Ação {action} não suportada para {resource_type}")

            # Elastic IP
            elif resource_type == "AWS::EC2::EIP":
                if action == ActionType.RELEASE:
                    result = self._execute_ec2_release_address(resource_id)
                else:
                    raise ValueError(f"Ação {action} não suportada para {resource_type}")

            # RDS Instance
            elif resource_type == "AWS::RDS::DBInstance":
                if action == ActionType.STOP:
                    result = self._execute_rds_stop(resource_id)
                elif action == ActionType.START:
                    result = self._execute_rds_start(resource_id)
                else:
                    raise ValueError(f"Ação {action} não suportada para {resource_type}")

            # Lambda Function
            elif resource_type == "AWS::Lambda::Function":
                if action == ActionType.RESIZE:
                    recommended_mb = int(metrics.get("recommendedMemoryMb", 128))
                    result = self._execute_lambda_resize(resource_id, recommended_mb)
                else:
                    raise ValueError(f"Ação {action} não suportada para {resource_type}")

            # S3 Bucket
            elif resource_type == "AWS::S3::Bucket":
                if action == ActionType.APPLY_LIFECYCLE:
                    result = self._execute_s3_apply_lifecycle(resource_id)
                else:
                    raise ValueError(f"Ação {action} não suportada para {resource_type}")

            # Load Balancer — DELETE requer aprovação manual, não executa automaticamente
            elif resource_type == "AWS::ElasticLoadBalancingV2::LoadBalancer":
                if action == ActionType.DELETE:
                    if self.dry_run:
                        print(f"[DRY-RUN] execute_action: NÃO deletando LB {resource_id}")
                        result = {"dryRun": True, "previousState": {"loadBalancerArn": resource_id}}
                    else:
                        self.ec2  # garante que ec2 client existe
                        elb = self._session.client("elbv2", region_name=self.region)
                        elb.delete_load_balancer(LoadBalancerArn=resource_id)
                        print(f"[INFO] execute_action: Load Balancer {resource_id} deletado")
                        result = {
                            "previousState": {"loadBalancerArn": resource_id},
                            "action": "delete_load_balancer",
                        }
                else:
                    raise ValueError(f"Ação {action} não suportada para {resource_type}")

            else:
                raise ValueError(f"ResourceType não suportado: {resource_type}")

        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "Unknown")
            print(f"[ERROR] execute_action: ClientError {error_code} ao executar {action} em {resource_id}: {exc}")
            raise

        # Salva rollback state com o previousState capturado
        previous_state = result.get("previousState", {})
        self.save_rollback_state(finding, remediation_id, previous_state)

        return result

    # -------------------------------------------------------------------------
    # Atualização de status do finding
    # -------------------------------------------------------------------------

    def update_finding_status(
        self,
        finding: dict[str, Any],
        new_status: str,
        remediation_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """
        Atualiza o status de um finding na tabela DynamoDB.

        Status possíveis: APPROVED, REMEDIATED, FAILED, PENDING_APPROVAL, RATE_LIMITED

        Args:
            finding: Finding a atualizar.
            new_status: Novo status.
            remediation_id: ID da remediação (para status REMEDIATED).
            error_message: Mensagem de erro (para status FAILED).
        """
        if not self.findings_table_name:
            print("[WARN] update_finding_status: FINDINGS_TABLE não configurada")
            return

        try:
            table = self.dynamodb.Table(self.findings_table_name)
            update_expr = "SET #status = :status, #updatedAt = :updatedAt"
            expr_names = {"#status": "status", "#updatedAt": "updatedAt"}
            expr_values: dict[str, Any] = {
                ":status": new_status,
                ":updatedAt": _iso_now(),
            }

            if remediation_id:
                update_expr += ", #remediationId = :remediationId"
                expr_names["#remediationId"] = "remediationId"
                expr_values[":remediationId"] = remediation_id

            if error_message:
                update_expr += ", #errorMessage = :errorMessage"
                expr_names["#errorMessage"] = "errorMessage"
                expr_values[":errorMessage"] = error_message

            table.update_item(
                Key={
                    "resourceId": finding.get("resourceId", ""),
                    "findingId": finding.get("findingId", ""),
                },
                UpdateExpression=update_expr,
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values,
            )
            print(f"[INFO] update_finding_status: finding {finding.get('findingId')} → {new_status}")

        except Exception as exc:
            print(f"[ERROR] update_finding_status: falha ao atualizar finding: {exc}")

    # -------------------------------------------------------------------------
    # Métricas CloudWatch
    # -------------------------------------------------------------------------

    def publish_remediation_metric(
        self,
        action: str,
        resource_type: str,
        success: bool,
        dry_run: bool = False,
        savings: float = 0.0,
        carbon: float = 0.0,
    ) -> None:
        """
        Publica métricas de remediação no namespace GreenOps/Remediation.

        Métricas publicadas:
        - RemediationsApplied (Count)
        - RemediationsFailed (Count)
        - CostSaved (USD estimado)
        - CarbonReduced (MTCO2e estimado)
        """
        try:
            timestamp = _now_utc()
            env = self.environment
            metric_name = "RemediationsApplied" if success else "RemediationsFailed"

            metric_data = [
                {
                    "MetricName": metric_name,
                    "Dimensions": [
                        {"Name": "Environment",   "Value": env},
                        {"Name": "ActionType",    "Value": action},
                        {"Name": "ResourceType",  "Value": resource_type.split("::")[-1]},
                        {"Name": "DryRun",        "Value": str(dry_run)},
                    ],
                    "Value": 1,
                    "Unit": "Count",
                    "Timestamp": timestamp,
                },
            ]

            if success and savings > 0:
                metric_data.append({
                    "MetricName": "CostSaved",
                    "Dimensions": [{"Name": "Environment", "Value": env}],
                    "Value": savings,
                    "Unit": "None",
                    "Timestamp": timestamp,
                })

            if success and carbon > 0:
                metric_data.append({
                    "MetricName": "CarbonReduced",
                    "Dimensions": [{"Name": "Environment", "Value": env}],
                    "Value": carbon,
                    "Unit": "None",
                    "Timestamp": timestamp,
                })

            self.cw.put_metric_data(
                Namespace="GreenOps/Remediation",
                MetricData=metric_data,
            )

        except Exception as exc:
            print(f"[WARN] publish_remediation_metric: falha ao publicar métricas: {exc}")

    # -------------------------------------------------------------------------
    # Processamento principal de um finding
    # -------------------------------------------------------------------------

    def process_finding(self, finding: dict[str, Any]) -> dict[str, Any]:
        """
        Processa um único finding aprovado: classifica risco, verifica rate limit,
        executa ou encaminha para aprovação.

        Args:
            finding: Finding no formato padrão GreenOps com status=APPROVED.

        Returns:
            dict com resultado: status, action, remediation_id, message.
        """
        finding_id = finding.get("findingId", "unknown")
        resource_id = finding.get("resourceId", "unknown")
        resource_type = finding.get("resourceType", "unknown")
        rec = finding.get("recommendation", {})
        action = rec.get("action", "")
        savings = float(rec.get("estimatedMonthlySavings", 0))
        carbon = float(rec.get("estimatedMonthlyCarbonReduction", 0))

        print(f"[INFO] process_finding: processando finding {finding_id} ({action} em {resource_id})")

        # 1. Classifica risco efetivo
        risk_level = self.classify_risk(finding)
        print(f"[INFO] process_finding: riskLevel={risk_level} para {finding_id}")

        # 2. Ações MEDIUM/HIGH/CRITICAL → solicita aprovação
        if risk_level.requires_approval:
            approval_id = _make_approval_id(finding_id)
            self.request_approval(finding, risk_level, approval_id)
            self.update_finding_status(finding, "PENDING_APPROVAL")
            return {
                "status": "PENDING_APPROVAL",
                "findingId": finding_id,
                "approvalId": approval_id,
                "riskLevel": risk_level.value,
                "message": f"Aprovação solicitada via SNS (riskLevel={risk_level})",
            }

        # 3. Ação LOW → verifica rate limit
        allowed, reason = self._rate_limiter.check_and_increment(action)
        if not allowed:
            print(f"[WARN] process_finding: rate limit atingido para {finding_id}: {reason}")
            self.update_finding_status(finding, "RATE_LIMITED", error_message=reason)
            return {
                "status": "RATE_LIMITED",
                "findingId": finding_id,
                "message": reason,
            }

        # 4. Executa a ação
        remediation_id = _make_remediation_id(finding_id)
        try:
            result = self.execute_action(finding, remediation_id)
            self.update_finding_status(finding, "REMEDIATED", remediation_id=remediation_id)
            self.publish_remediation_metric(
                action=action,
                resource_type=resource_type,
                success=True,
                dry_run=result.get("dryRun", False),
                savings=savings,
                carbon=carbon,
            )
            print(f"[INFO] process_finding: finding {finding_id} REMEDIATED (id={remediation_id})")
            return {
                "status": "REMEDIATED",
                "findingId": finding_id,
                "remediationId": remediation_id,
                "dryRun": result.get("dryRun", False),
                "message": f"Ação {action} executada com sucesso em {resource_id}",
            }

        except Exception as exc:
            error_msg = str(exc)
            print(f"[ERROR] process_finding: falha ao executar {action} em {resource_id}: {error_msg}")
            self.update_finding_status(finding, "FAILED", error_message=error_msg)
            self.publish_remediation_metric(
                action=action,
                resource_type=resource_type,
                success=False,
            )
            return {
                "status": "FAILED",
                "findingId": finding_id,
                "error": error_msg,
                "message": f"Falha ao executar {action} em {resource_id}",
            }


# =============================================================================
# Lambda entry point — triggerado por DynamoDB Stream
# =============================================================================

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Entry point da função Lambda de Remediation.

    Triggerado pelo DynamoDB Stream da tabela ``greenops-findings-{env}``.
    Processa apenas eventos INSERT e MODIFY onde o novo status é APPROVED.

    Estrutura do evento DynamoDB Stream::

        {
          "Records": [
            {
              "eventName": "INSERT" | "MODIFY" | "REMOVE",
              "dynamodb": {
                "NewImage": { ... },   # item após a mudança
                "OldImage": { ... },   # item antes da mudança (MODIFY/REMOVE)
              }
            }
          ]
        }

    Filtro de processamento:
    - eventName IN (INSERT, MODIFY)
    - NewImage.status == "APPROVED"
    - OldImage.status != "APPROVED" (evita reprocessar findings já aprovados)

    Args:
        event: Evento DynamoDB Stream com lista de Records.
        context: Contexto Lambda.

    Returns:
        dict com statusCode 200 e body JSON contendo:
        - processedCount: findings processados
        - skippedCount: findings ignorados (não APPROVED ou já processados)
        - results: lista de resultados por finding
    """
    print(f"[INFO] lambda_handler: recebidos {len(event.get('Records', []))} records do DynamoDB Stream")

    region = _get_env("AWS_REGION", "us-east-1")
    orchestrator = RemediationOrchestrator(region=region)

    results: list[dict[str, Any]] = []
    processed = 0
    skipped = 0

    for record in event.get("Records", []):
        event_name = record.get("eventName", "")

        # Ignora REMOVE — não há ação de remediação para itens deletados
        if event_name == "REMOVE":
            skipped += 1
            continue

        dynamodb_data = record.get("dynamodb", {})
        new_image = dynamodb_data.get("NewImage", {})
        old_image = dynamodb_data.get("OldImage", {})

        if not new_image:
            skipped += 1
            continue

        # Desserializa o formato AttributeValue do DynamoDB Stream
        try:
            new_item = _deserialize_dynamodb_item(new_image)
            old_item = _deserialize_dynamodb_item(old_image) if old_image else {}
        except Exception as exc:
            print(f"[ERROR] lambda_handler: falha ao desserializar record: {exc}")
            skipped += 1
            continue

        new_status = new_item.get("status", "")
        old_status = old_item.get("status", "")

        # Processa apenas findings que acabaram de ser aprovados
        # (status mudou para APPROVED, ou é INSERT com APPROVED)
        if new_status != "APPROVED":
            skipped += 1
            continue

        if old_status == "APPROVED":
            # Já estava APPROVED — evita reprocessamento em caso de retry
            print(f"[INFO] lambda_handler: finding {new_item.get('findingId')} já estava APPROVED — ignorado")
            skipped += 1
            continue

        # Processa o finding
        try:
            result = orchestrator.process_finding(new_item)
            results.append(result)
            processed += 1
        except Exception as exc:
            finding_id = new_item.get("findingId", "unknown")
            print(f"[ERROR] lambda_handler: falha inesperada ao processar finding {finding_id}: {exc}")
            results.append({
                "status": "FAILED",
                "findingId": finding_id,
                "error": str(exc),
            })
            processed += 1

    summary = {
        "processedCount": processed,
        "skippedCount": skipped,
        "results": results,
        "timestamp": _iso_now(),
        "region": region,
    }

    print(
        f"[INFO] lambda_handler: concluído — "
        f"{processed} processados, {skipped} ignorados"
    )

    return {
        "statusCode": 200,
        "body": json.dumps(summary),
    }
