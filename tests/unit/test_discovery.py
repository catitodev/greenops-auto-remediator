"""
tests/unit/test_discovery.py
============================
Testes unitários para o módulo discovery.handler usando moto para mock AWS.

Cobertura:
- scan_ec2_instances: detecta instância idle (CPU < 5%)
- scan_ec2_instances: ignora instâncias sem tag GreenOpsManaged=true
- scan_ec2_instances: gera finding CRITICAL para instância de Production
- scan_ec2_instances: não gera finding quando CPU >= 5%
- scan_ec2_instances: não gera finding quando CloudWatch não tem dados
- scan_ebs_volumes: detecta volume orphaned (desanexado > 30 dias)
- scan_ebs_volumes: ignora volume criado recentemente
- scan_elastic_ips: detecta EIP não associado
- scan_elastic_ips: ignora EIP associado a instância
- _get_cloudwatch_average: retorna valor correto com datapoints
- _get_cloudwatch_average: retorna None sem datapoints
- _is_production_resource: detecta tag Environment=Production
- _build_finding: calcula priorityScore e preenche todos os campos
- parse_tags: converte lista AWS para dict
- lambda_handler: retorna 200 com contagem de findings
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Configura PYTHONPATH para importar módulos src/
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

# Variáveis de ambiente necessárias antes de importar o módulo
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("FINDINGS_TTL_DAYS", "90")
os.environ.setdefault("FINDINGS_TABLE", "greenops-findings-test")
os.environ.setdefault("ENVIRONMENT", "dev")

import discovery.handler as dh
from shared.constants import ActionType, RiskLevel, Severity, WasteType
from shared.utils import parse_tags

# ---------------------------------------------------------------------------
# Constantes de teste
# ---------------------------------------------------------------------------

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"

# Tags padrão para recursos elegíveis
TAGS_MANAGED = [{"Key": "GreenOpsManaged", "Value": "true"}, {"Key": "Environment", "Value": "dev"}]
TAGS_PRODUCTION = [{"Key": "GreenOpsManaged", "Value": "true"}, {"Key": "Environment", "Value": "Production"}]
TAGS_NO_GREENOPS = [{"Key": "Environment", "Value": "dev"}]  # sem GreenOpsManaged


# ---------------------------------------------------------------------------
# Fixtures de infraestrutura
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def aws_credentials():
    """Garante que as credenciais AWS falsas estão configuradas para moto."""
    os.environ["AWS_DEFAULT_REGION"] = REGION
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"


@pytest.fixture(scope="function")
def moto_session(aws_credentials):
    """
    Sessão boto3 dentro do contexto moto.
    Usada para criar recursos de teste e instanciar o DiscoveryOrchestrator.
    """
    with mock_aws():
        session = boto3.Session(region_name=REGION)
        yield session


@pytest.fixture(scope="function")
def orchestrator(moto_session):
    """
    Instância de DiscoveryOrchestrator com sessão moto.
    Compute Optimizer é mockado pois moto não o suporta.
    """
    orch = dh.DiscoveryOrchestrator(region=REGION, session=moto_session)
    # Compute Optimizer não é suportado pelo moto — mock para retornar vazio
    co_mock = MagicMock()
    co_mock.get_ec2_instance_recommendations.return_value = {"instanceRecommendations": []}
    orch._co = co_mock
    return orch


@pytest.fixture(scope="function")
def ec2_client(moto_session):
    """Cliente EC2 dentro do contexto moto."""
    return moto_session.client("ec2", region_name=REGION)


@pytest.fixture(scope="function")
def dynamodb_resource(moto_session):
    """Recurso DynamoDB com tabela de findings criada."""
    dynamodb = moto_session.resource("dynamodb", region_name=REGION)
    table = dynamodb.create_table(
        TableName="greenops-findings-test",
        KeySchema=[
            {"AttributeName": "resourceId", "KeyType": "HASH"},
            {"AttributeName": "findingId",  "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "resourceId", "AttributeType": "S"},
            {"AttributeName": "findingId",  "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    yield dynamodb


# ---------------------------------------------------------------------------
# Helpers de setup de recursos AWS
# ---------------------------------------------------------------------------

def _create_ec2_instance(
    ec2_client: Any,
    instance_type: str = "t3.medium",
    tags: list[dict[str, str]] | None = None,
    state: str = "running",
) -> str:
    """Cria uma instância EC2 e retorna o InstanceId."""
    resp = ec2_client.run_instances(
        ImageId="ami-12345678",
        MinCount=1,
        MaxCount=1,
        InstanceType=instance_type,
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": tags or TAGS_MANAGED,
        }],
    )
    instance_id = resp["Instances"][0]["InstanceId"]

    # moto inicia instâncias em 'pending' → aguarda 'running'
    # Para testes, o estado já é 'running' após run_instances no moto
    return instance_id


def _mock_cloudwatch_cpu(
    cw_client: Any,
    instance_id: str,
    avg_cpu: float,
) -> None:
    """
    Injeta um datapoint de CPUUtilization no CloudWatch mockado.

    moto suporta put_metric_data mas get_metric_statistics retorna
    os dados inseridos. Usamos patch direto para controlar o retorno.
    """
    # Não é necessário chamar put_metric_data — usamos patch no teste


# ---------------------------------------------------------------------------
# Testes: _is_production_resource
# ---------------------------------------------------------------------------

class TestIsProductionResource:
    """Testa a função helper _is_production_resource."""

    def test_production_tag_exact(self):
        """Tag Environment=Production deve retornar True."""
        assert dh._is_production_resource({"Environment": "Production"}) is True

    def test_production_tag_lowercase(self):
        """Tag Environment=production (lowercase) deve retornar True."""
        assert dh._is_production_resource({"Environment": "production"}) is True

    def test_production_tag_mixed_case(self):
        """Tag Environment=PRODUCTION deve retornar True."""
        assert dh._is_production_resource({"Environment": "PRODUCTION"}) is True

    def test_dev_environment(self):
        """Tag Environment=dev não é produção."""
        assert dh._is_production_resource({"Environment": "dev"}) is False

    def test_staging_environment(self):
        """Tag Environment=staging não é produção."""
        assert dh._is_production_resource({"Environment": "staging"}) is False

    def test_no_environment_tag(self):
        """Ausência de tag Environment não é produção."""
        assert dh._is_production_resource({}) is False

    def test_other_tags_only(self):
        """Tags sem Environment não é produção."""
        assert dh._is_production_resource({"GreenOpsManaged": "true"}) is False


# ---------------------------------------------------------------------------
# Testes: parse_tags (via shared.utils)
# ---------------------------------------------------------------------------

class TestParseTags:
    """Testa a conversão de lista de tags AWS para dict."""

    def test_standard_aws_format(self):
        """Converte lista AWS padrão para dict."""
        aws_tags = [
            {"Key": "GreenOpsManaged", "Value": "true"},
            {"Key": "Environment",     "Value": "dev"},
            {"Key": "Team",            "Value": "platform"},
        ]
        result = parse_tags(aws_tags)
        assert result == {
            "GreenOpsManaged": "true",
            "Environment": "dev",
            "Team": "platform",
        }

    def test_empty_list(self):
        """Lista vazia retorna dict vazio."""
        assert parse_tags([]) == {}

    def test_none_input(self):
        """None retorna dict vazio (com aviso no log)."""
        assert parse_tags(None) == {}

    def test_malformed_item_ignored(self):
        """Item sem Key ou Value é ignorado silenciosamente."""
        tags = [
            {"Key": "Valid", "Value": "yes"},
            {"NoKey": "bad"},
            {"Key": "OnlyKey"},
        ]
        result = parse_tags(tags)
        assert result == {"Valid": "yes"}

    def test_non_dict_item_ignored(self):
        """Item que não é dict é ignorado."""
        tags = [{"Key": "A", "Value": "1"}, "not-a-dict", 42]
        result = parse_tags(tags)
        assert result == {"A": "1"}

    def test_single_tag(self):
        """Lista com uma tag."""
        result = parse_tags([{"Key": "Env", "Value": "prod"}])
        assert result == {"Env": "prod"}

    def test_values_converted_to_str(self):
        """Valores são convertidos para string."""
        result = parse_tags([{"Key": "Count", "Value": "42"}])
        assert isinstance(result["Count"], str)
        assert result["Count"] == "42"


# ---------------------------------------------------------------------------
# Testes: _get_cloudwatch_average
# ---------------------------------------------------------------------------

class TestGetCloudwatchAverage:
    """Testa a função helper _get_cloudwatch_average."""

    def test_returns_average_when_datapoints_exist(self, moto_session):
        """Retorna o valor Average quando há datapoints."""
        cw = moto_session.client("cloudwatch", region_name=REGION)

        # Injeta datapoint via put_metric_data
        cw.put_metric_data(
            Namespace="AWS/EC2",
            MetricData=[{
                "MetricName": "CPUUtilization",
                "Dimensions": [{"Name": "InstanceId", "Value": "i-test001"}],
                "Value": 2.5,
                "Unit": "Percent",
                "Timestamp": datetime.now(tz=timezone.utc),
            }],
        )

        # Mock direto do get_metric_statistics para retornar valor controlado
        with patch.object(cw, "get_metric_statistics", return_value={
            "Datapoints": [{"Average": 2.5, "Timestamp": datetime.now(tz=timezone.utc)}],
        }):
            result = dh._get_cloudwatch_average(
                cw_client=cw,
                namespace="AWS/EC2",
                metric_name="CPUUtilization",
                dimensions=[{"Name": "InstanceId", "Value": "i-test001"}],
                days=7,
            )

        assert result == 2.5

    def test_returns_none_when_no_datapoints(self, moto_session):
        """Retorna None quando não há datapoints."""
        cw = moto_session.client("cloudwatch", region_name=REGION)

        with patch.object(cw, "get_metric_statistics", return_value={"Datapoints": []}):
            result = dh._get_cloudwatch_average(
                cw_client=cw,
                namespace="AWS/EC2",
                metric_name="CPUUtilization",
                dimensions=[{"Name": "InstanceId", "Value": "i-nonexistent"}],
                days=7,
            )

        assert result is None

    def test_returns_none_on_client_error(self, moto_session):
        """Retorna None (não lança exceção) em caso de ClientError."""
        from botocore.exceptions import ClientError
        cw = moto_session.client("cloudwatch", region_name=REGION)

        error_response = {"Error": {"Code": "InvalidParameterValue", "Message": "test"}}
        with patch.object(cw, "get_metric_statistics", side_effect=ClientError(error_response, "GetMetricStatistics")):
            result = dh._get_cloudwatch_average(
                cw_client=cw,
                namespace="AWS/EC2",
                metric_name="CPUUtilization",
                dimensions=[{"Name": "InstanceId", "Value": "i-error"}],
                days=7,
            )

        assert result is None

    def test_uses_sum_stat_when_specified(self, moto_session):
        """Usa a estatística Sum quando especificada."""
        cw = moto_session.client("cloudwatch", region_name=REGION)

        with patch.object(cw, "get_metric_statistics", return_value={
            "Datapoints": [{"Sum": 100.0, "Timestamp": datetime.now(tz=timezone.utc)}],
        }):
            result = dh._get_cloudwatch_average(
                cw_client=cw,
                namespace="AWS/Lambda",
                metric_name="Invocations",
                dimensions=[{"Name": "FunctionName", "Value": "my-fn"}],
                days=7,
                stat="Sum",
            )

        assert result == 100.0


# ---------------------------------------------------------------------------
# Testes: _build_finding
# ---------------------------------------------------------------------------

class TestBuildFinding:
    """Testa a construção do finding no formato padrão GreenOps."""

    def test_all_required_fields_present(self):
        """Finding deve conter todos os campos obrigatórios."""
        finding = dh._build_finding(
            resource_type="AWS::EC2::Instance",
            resource_id="i-abc123",
            region=REGION,
            waste_type=WasteType.IDLE,
            severity=Severity.HIGH,
            description="Instância idle de teste",
            metrics={"avgCpuUtilizationPct": 2.3},
            action=ActionType.STOP,
            reason="CPU abaixo de 5% por 7 dias",
            estimated_savings=50.0,
            estimated_carbon=0.08,
            risk_level=RiskLevel.MEDIUM,
            tags={"GreenOpsManaged": "true"},
            confidence=0.85,
        )

        required_fields = [
            "findingId", "timestamp", "resourceType", "resourceId",
            "region", "wasteType", "severity", "description",
            "metrics", "recommendation", "tags", "priorityScore", "ttl",
        ]
        for field in required_fields:
            assert field in finding, f"Campo obrigatório ausente: {field}"

    def test_recommendation_fields_present(self):
        """Recommendation deve conter todos os subcampos."""
        finding = dh._build_finding(
            resource_type="AWS::EC2::Instance",
            resource_id="i-abc123",
            region=REGION,
            waste_type=WasteType.IDLE,
            severity=Severity.HIGH,
            description="Teste",
            metrics={},
            action=ActionType.STOP,
            reason="Teste",
            estimated_savings=50.0,
            estimated_carbon=0.08,
            risk_level=RiskLevel.MEDIUM,
            tags={},
        )

        rec = finding["recommendation"]
        assert rec["action"] == ActionType.STOP
        assert rec["estimatedMonthlySavings"] == 50.0
        assert rec["estimatedMonthlyCarbonReduction"] == 0.08
        assert rec["riskLevel"] == RiskLevel.MEDIUM

    def test_priority_score_calculated_correctly(self):
        """PriorityScore deve ser calculado pela fórmula correta."""
        # savings=300, carbon=2.0, severity=HIGH(75), confidence=0.9
        # score = (300/500*0.40 + 2.0/5.0*0.30 + 75/100*0.20 + 0.9*0.10) * 100
        #       = (0.24 + 0.12 + 0.15 + 0.09) * 100 = 60.0
        finding = dh._build_finding(
            resource_type="AWS::EC2::Instance",
            resource_id="i-score-test",
            region=REGION,
            waste_type=WasteType.IDLE,
            severity=Severity.HIGH,
            description="Score test",
            metrics={},
            action=ActionType.STOP,
            reason="Test",
            estimated_savings=300.0,
            estimated_carbon=2.0,
            risk_level=RiskLevel.MEDIUM,
            tags={},
            confidence=0.9,
        )

        assert finding["priorityScore"] == 60.0

    def test_finding_id_is_deterministic(self):
        """Mesmo recurso no mesmo dia gera o mesmo findingId."""
        kwargs = dict(
            resource_type="AWS::EC2::Instance",
            resource_id="i-deterministic",
            region=REGION,
            waste_type=WasteType.IDLE,
            severity=Severity.HIGH,
            description="Test",
            metrics={},
            action=ActionType.STOP,
            reason="Test",
            estimated_savings=50.0,
            estimated_carbon=0.08,
            risk_level=RiskLevel.MEDIUM,
            tags={},
        )
        f1 = dh._build_finding(**kwargs)
        f2 = dh._build_finding(**kwargs)
        assert f1["findingId"] == f2["findingId"]

    def test_waste_type_serialized_as_string(self):
        """wasteType deve ser string no finding (não enum)."""
        finding = dh._build_finding(
            resource_type="AWS::EC2::Volume",
            resource_id="vol-001",
            region=REGION,
            waste_type=WasteType.ORPHAN,
            severity=Severity.MEDIUM,
            description="Test",
            metrics={},
            action=ActionType.DELETE,
            reason="Test",
            estimated_savings=8.0,
            estimated_carbon=0.01,
            risk_level=RiskLevel.CRITICAL,
            tags={},
        )
        assert finding["wasteType"] == "ORPHAN"
        assert isinstance(finding["wasteType"], str)

    def test_ttl_is_in_future(self):
        """TTL deve ser um timestamp no futuro."""
        finding = dh._build_finding(
            resource_type="AWS::EC2::Instance",
            resource_id="i-ttl-test",
            region=REGION,
            waste_type=WasteType.IDLE,
            severity=Severity.LOW,
            description="Test",
            metrics={},
            action=ActionType.STOP,
            reason="Test",
            estimated_savings=10.0,
            estimated_carbon=0.01,
            risk_level=RiskLevel.LOW,
            tags={},
        )
        import time
        assert finding["ttl"] > int(time.time())

    def test_tags_preserved_in_finding(self):
        """Tags do recurso devem ser preservadas no finding."""
        tags = {"GreenOpsManaged": "true", "Team": "platform", "CostCenter": "eng-001"}
        finding = dh._build_finding(
            resource_type="AWS::EC2::Instance",
            resource_id="i-tags-test",
            region=REGION,
            waste_type=WasteType.IDLE,
            severity=Severity.HIGH,
            description="Test",
            metrics={},
            action=ActionType.STOP,
            reason="Test",
            estimated_savings=50.0,
            estimated_carbon=0.08,
            risk_level=RiskLevel.MEDIUM,
            tags=tags,
        )
        assert finding["tags"] == tags


# ---------------------------------------------------------------------------
# Testes: scan_ec2_instances — instância idle
# ---------------------------------------------------------------------------

class TestScanEC2InstancesIdle:
    """Testa a detecção de instâncias EC2 idle (CPU < 5%)."""

    def test_finds_idle_instance_below_threshold(self, orchestrator, ec2_client):
        """
        Instância com CPU médio de 2.3% deve gerar finding IDLE.

        Setup:
        - Cria instância EC2 com tag GreenOpsManaged=true
        - Mocka CloudWatch para retornar CPU=2.3% (abaixo do threshold de 5%)

        Expectativa:
        - 1 finding gerado
        - wasteType=IDLE, action=STOP, severity=HIGH
        """
        instance_id = _create_ec2_instance(ec2_client, tags=TAGS_MANAGED)

        with patch.object(orchestrator.cw, "get_metric_statistics", return_value={
            "Datapoints": [{"Average": 2.3, "Timestamp": datetime.now(tz=timezone.utc)}],
        }):
            findings = orchestrator.scan_ec2_instances()

        assert len(findings) == 1
        f = findings[0]
        assert f["resourceId"] == instance_id
        assert f["wasteType"] == WasteType.IDLE
        assert f["resourceType"] == "AWS::EC2::Instance"
        assert f["recommendation"]["action"] == ActionType.STOP
        assert f["severity"] == Severity.HIGH
        assert f["recommendation"]["riskLevel"] == RiskLevel.MEDIUM
        assert f["metrics"]["avgCpuUtilizationPct"] == 2.3
        assert f["metrics"]["observationDays"] == dh.IDLE_DAYS_THRESHOLD
        assert f["priorityScore"] > 0

    def test_no_finding_when_cpu_above_threshold(self, orchestrator, ec2_client):
        """
        Instância com CPU médio de 8% (acima de 5%) não deve gerar finding.
        """
        _create_ec2_instance(ec2_client, tags=TAGS_MANAGED)

        with patch.object(orchestrator.cw, "get_metric_statistics", return_value={
            "Datapoints": [{"Average": 8.0, "Timestamp": datetime.now(tz=timezone.utc)}],
        }):
            findings = orchestrator.scan_ec2_instances()

        assert len(findings) == 0

    def test_no_finding_when_cpu_exactly_at_threshold(self, orchestrator, ec2_client):
        """
        CPU exatamente em 5.0% não deve gerar finding (threshold é estrito <).
        """
        _create_ec2_instance(ec2_client, tags=TAGS_MANAGED)

        with patch.object(orchestrator.cw, "get_metric_statistics", return_value={
            "Datapoints": [{"Average": 5.0, "Timestamp": datetime.now(tz=timezone.utc)}],
        }):
            findings = orchestrator.scan_ec2_instances()

        assert len(findings) == 0

    def test_no_finding_when_no_cloudwatch_data(self, orchestrator, ec2_client):
        """
        Sem dados CloudWatch (None), não deve gerar finding.
        Evita falsos positivos em instâncias recém-criadas.
        """
        _create_ec2_instance(ec2_client, tags=TAGS_MANAGED)

        with patch.object(orchestrator.cw, "get_metric_statistics", return_value={
            "Datapoints": [],
        }):
            findings = orchestrator.scan_ec2_instances()

        assert len(findings) == 0

    def test_no_finding_for_instance_without_greenops_tag(self, orchestrator, ec2_client):
        """
        Instância sem tag GreenOpsManaged=true deve ser ignorada.
        O filtro é aplicado na chamada describe_instances.
        """
        # Cria instância SEM a tag GreenOpsManaged
        ec2_client.run_instances(
            ImageId="ami-12345678",
            MinCount=1,
            MaxCount=1,
            InstanceType="t3.medium",
            TagSpecifications=[{
                "ResourceType": "instance",
                "Tags": TAGS_NO_GREENOPS,
            }],
        )

        with patch.object(orchestrator.cw, "get_metric_statistics", return_value={
            "Datapoints": [{"Average": 1.0, "Timestamp": datetime.now(tz=timezone.utc)}],
        }):
            findings = orchestrator.scan_ec2_instances()

        assert len(findings) == 0

    def test_production_instance_gets_critical_risk(self, orchestrator, ec2_client):
        """
        Instância com tag Environment=Production deve gerar finding com
        riskLevel=CRITICAL e severity=CRITICAL (proteção de produção).
        """
        instance_id = _create_ec2_instance(ec2_client, tags=TAGS_PRODUCTION)

        with patch.object(orchestrator.cw, "get_metric_statistics", return_value={
            "Datapoints": [{"Average": 1.5, "Timestamp": datetime.now(tz=timezone.utc)}],
        }):
            findings = orchestrator.scan_ec2_instances()

        assert len(findings) == 1
        f = findings[0]
        assert f["resourceId"] == instance_id
        assert f["recommendation"]["riskLevel"] == RiskLevel.CRITICAL
        assert f["severity"] == Severity.CRITICAL
        assert f["wasteType"] == WasteType.IDLE

    def test_multiple_instances_only_idle_ones_flagged(self, orchestrator, ec2_client):
        """
        Com 3 instâncias, apenas as idle devem gerar findings.
        """
        id_idle_1 = _create_ec2_instance(ec2_client, tags=TAGS_MANAGED)
        id_idle_2 = _create_ec2_instance(ec2_client, tags=TAGS_MANAGED)
        id_active = _create_ec2_instance(ec2_client, tags=TAGS_MANAGED)

        call_count = [0]

        def mock_get_metrics(*args, **kwargs):
            """Retorna CPU baixo para as 2 primeiras instâncias, alto para a 3ª."""
            dims = kwargs.get("Dimensions", [])
            instance_id = next(
                (d["Value"] for d in dims if d["Name"] == "InstanceId"), ""
            )
            call_count[0] += 1
            if instance_id in (id_idle_1, id_idle_2):
                return {"Datapoints": [{"Average": 2.0, "Timestamp": datetime.now(tz=timezone.utc)}]}
            return {"Datapoints": [{"Average": 15.0, "Timestamp": datetime.now(tz=timezone.utc)}]}

        with patch.object(orchestrator.cw, "get_metric_statistics", side_effect=mock_get_metrics):
            findings = orchestrator.scan_ec2_instances()

        assert len(findings) == 2
        finding_ids = {f["resourceId"] for f in findings}
        assert id_idle_1 in finding_ids
        assert id_idle_2 in finding_ids
        assert id_active not in finding_ids

    def test_t3_medium_uses_correct_cost_estimate(self, orchestrator, ec2_client):
        """
        Instância t3.medium deve usar estimativa de custo específica ($30/mês).
        """
        _create_ec2_instance(ec2_client, instance_type="t3.medium", tags=TAGS_MANAGED)

        with patch.object(orchestrator.cw, "get_metric_statistics", return_value={
            "Datapoints": [{"Average": 1.0, "Timestamp": datetime.now(tz=timezone.utc)}],
        }):
            findings = orchestrator.scan_ec2_instances()

        assert len(findings) == 1
        assert findings[0]["recommendation"]["estimatedMonthlySavings"] == 30.0
        assert findings[0]["recommendation"]["estimatedMonthlyCarbonReduction"] == 0.05

    def test_scan_continues_when_cloudwatch_raises_exception(self, orchestrator, ec2_client):
        """
        Exceção no CloudWatch não deve interromper o scan inteiro.
        O scan deve retornar lista vazia sem propagar a exceção.
        """
        _create_ec2_instance(ec2_client, tags=TAGS_MANAGED)

        from botocore.exceptions import ClientError
        error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "GetMetricStatistics",
        )
        with patch.object(orchestrator.cw, "get_metric_statistics", side_effect=error):
            # Não deve lançar exceção
            findings = orchestrator.scan_ec2_instances()

        assert isinstance(findings, list)


# ---------------------------------------------------------------------------
# Testes: scan_ebs_volumes
# ---------------------------------------------------------------------------

class TestScanEBSVolumes:
    """Testa a detecção de volumes EBS orphaned."""

    def _make_paginator_mock(self, volumes: list[dict[str, Any]]) -> MagicMock:
        """Helper: cria mock de paginator que retorna a lista de volumes."""
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Volumes": volumes}]
        return mock_paginator

    def test_finds_orphaned_volume_old_enough(self, orchestrator):
        """
        Volume desanexado criado há mais de 30 dias deve gerar finding ORPHAN.

        Usa patch direto no client EC2 já instanciado pelo orchestrator
        para evitar problemas de inicialização lazy com moto.
        """
        volume_id = "vol-orphan001"
        old_create_time = datetime.now(tz=timezone.utc) - timedelta(days=45)
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)

        volumes = [{
            "VolumeId": volume_id,
            "Size": 50,
            "VolumeType": "gp2",
            "Iops": 150,
            "CreateTime": old_create_time,
            "Tags": TAGS_MANAGED,
        }]

        # Força inicialização do client EC2 antes do patch
        _ = orchestrator.ec2

        with patch.object(orchestrator._ec2, "get_paginator",
                          return_value=self._make_paginator_mock(volumes)):
            with patch("discovery.handler._days_ago", return_value=cutoff):
                findings = orchestrator.scan_ebs_volumes()

        assert len(findings) == 1
        f = findings[0]
        assert f["resourceId"] == volume_id
        assert f["wasteType"] == WasteType.ORPHAN
        assert f["resourceType"] == "AWS::EC2::Volume"
        assert f["recommendation"]["action"] == ActionType.DELETE
        assert f["recommendation"]["riskLevel"] == RiskLevel.CRITICAL
        assert f["metrics"]["sizeGb"] == 50
        assert f["metrics"]["volumeType"] == "gp2"

    def test_ignores_recently_created_volume(self, orchestrator):
        """
        Volume criado há menos de 30 dias não deve gerar finding.
        """
        recent_date = datetime.now(tz=timezone.utc) - timedelta(days=5)
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)

        volumes = [{
            "VolumeId": "vol-recent",
            "Size": 100,
            "VolumeType": "gp2",
            "Iops": 300,
            "CreateTime": recent_date,
            "Tags": TAGS_MANAGED,
        }]

        _ = orchestrator.ec2

        with patch.object(orchestrator._ec2, "get_paginator",
                          return_value=self._make_paginator_mock(volumes)):
            with patch("discovery.handler._days_ago", return_value=cutoff):
                findings = orchestrator.scan_ebs_volumes()

        assert len(findings) == 0

    def test_cost_estimate_based_on_size(self, orchestrator, ec2_client):
        """
        Custo estimado deve ser calculado com base no tamanho do volume.
        gp2 = $0.10/GB/mês → 100GB = $10.00/mês

        Usa ec2_client fixture para garantir que o client EC2 do orchestrator
        já está inicializado dentro do contexto moto antes do patch.
        """
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=60)

        volumes = [{
            "VolumeId": "vol-100gb",
            "Size": 100,
            "VolumeType": "gp2",
            "Iops": 300,
            "CreateTime": datetime.now(tz=timezone.utc) - timedelta(days=45),
            "Tags": TAGS_MANAGED,
        }]

        # Força inicialização do client EC2 antes do patch
        _ = orchestrator.ec2

        with patch.object(orchestrator._ec2, "get_paginator",
                          return_value=self._make_paginator_mock(volumes)):
            with patch("discovery.handler._days_ago", return_value=cutoff):
                findings = orchestrator.scan_ebs_volumes()

        assert len(findings) == 1
        assert findings[0]["recommendation"]["estimatedMonthlySavings"] == 10.0


# ---------------------------------------------------------------------------
# Testes: scan_elastic_ips
# ---------------------------------------------------------------------------

class TestScanElasticIPs:
    """Testa a detecção de Elastic IPs não associados."""

    def test_finds_unassociated_eip(self, orchestrator, ec2_client):
        """
        EIP sem AssociationId e sem NetworkInterfaceId deve gerar finding ORPHAN.
        """
        # Aloca um EIP
        alloc_resp = ec2_client.allocate_address(Domain="vpc")
        allocation_id = alloc_resp["AllocationId"]

        # Adiciona tag GreenOpsManaged=true
        ec2_client.create_tags(
            Resources=[allocation_id],
            Tags=TAGS_MANAGED,
        )

        findings = orchestrator.scan_elastic_ips()

        assert len(findings) == 1
        f = findings[0]
        assert f["resourceId"] == allocation_id
        assert f["wasteType"] == WasteType.ORPHAN
        assert f["resourceType"] == "AWS::EC2::EIP"
        assert f["recommendation"]["action"] == ActionType.RELEASE
        assert f["recommendation"]["estimatedMonthlySavings"] == 3.6
        # Confiança deve ser 1.0 (certeza absoluta)
        assert f["priorityScore"] > 0

    def test_ignores_eip_without_greenops_tag(self, orchestrator, ec2_client):
        """
        EIP sem tag GreenOpsManaged=true deve ser ignorado.
        """
        alloc_resp = ec2_client.allocate_address(Domain="vpc")
        allocation_id = alloc_resp["AllocationId"]
        # Sem tag GreenOpsManaged

        findings = orchestrator.scan_elastic_ips()
        assert len(findings) == 0

    def test_production_eip_gets_critical_risk(self, orchestrator, ec2_client):
        """
        EIP com tag Environment=Production deve ter riskLevel=CRITICAL.
        """
        alloc_resp = ec2_client.allocate_address(Domain="vpc")
        allocation_id = alloc_resp["AllocationId"]
        ec2_client.create_tags(
            Resources=[allocation_id],
            Tags=TAGS_PRODUCTION,
        )

        findings = orchestrator.scan_elastic_ips()

        assert len(findings) == 1
        assert findings[0]["recommendation"]["riskLevel"] == RiskLevel.CRITICAL

    def test_multiple_eips_all_unassociated(self, orchestrator, ec2_client):
        """
        Múltiplos EIPs não associados devem gerar um finding cada.
        """
        ids = []
        for _ in range(3):
            resp = ec2_client.allocate_address(Domain="vpc")
            aid = resp["AllocationId"]
            ec2_client.create_tags(Resources=[aid], Tags=TAGS_MANAGED)
            ids.append(aid)

        findings = orchestrator.scan_elastic_ips()
        assert len(findings) == 3
        finding_ids = {f["resourceId"] for f in findings}
        for aid in ids:
            assert aid in finding_ids


# ---------------------------------------------------------------------------
# Testes: _to_dynamodb_item
# ---------------------------------------------------------------------------

class TestToDynamodbItem:
    """Testa a conversão de floats para Decimal."""

    def test_float_converted_to_decimal(self):
        """Floats devem ser convertidos para Decimal."""
        from decimal import Decimal
        item = dh._to_dynamodb_item({"savings": 50.5, "carbon": 0.08})
        assert isinstance(item["savings"], Decimal)
        assert isinstance(item["carbon"], Decimal)
        assert item["savings"] == Decimal("50.5")

    def test_nested_floats_converted(self):
        """Floats em dicts aninhados devem ser convertidos."""
        from decimal import Decimal
        item = dh._to_dynamodb_item({
            "recommendation": {"estimatedMonthlySavings": 30.0},
            "priorityScore": 60.0,
        })
        assert isinstance(item["recommendation"]["estimatedMonthlySavings"], Decimal)
        assert isinstance(item["priorityScore"], Decimal)

    def test_strings_and_ints_unchanged(self):
        """Strings e ints não devem ser alterados."""
        item = dh._to_dynamodb_item({"name": "test", "count": 5, "active": True})
        assert item["name"] == "test"
        assert item["count"] == 5
        assert item["active"] is True

    def test_list_floats_converted(self):
        """Floats em listas devem ser convertidos."""
        from decimal import Decimal
        item = dh._to_dynamodb_item({"scores": [1.5, 2.5, 3.5]})
        assert all(isinstance(s, Decimal) for s in item["scores"])


# ---------------------------------------------------------------------------
# Testes: lambda_handler
# ---------------------------------------------------------------------------

class TestLambdaHandler:
    """Testa o entry point lambda_handler."""

    def test_returns_200_with_findings_count(self, moto_session):
        """
        lambda_handler deve retornar statusCode=200 com contagem de findings.
        """
        os.environ["FINDINGS_TABLE"] = "greenops-findings-test"
        os.environ["ENVIRONMENT"] = "dev"

        # Cria tabela DynamoDB
        dynamodb = moto_session.resource("dynamodb", region_name=REGION)
        try:
            table = dynamodb.create_table(
                TableName="greenops-findings-test",
                KeySchema=[
                    {"AttributeName": "resourceId", "KeyType": "HASH"},
                    {"AttributeName": "findingId",  "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "resourceId", "AttributeType": "S"},
                    {"AttributeName": "findingId",  "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            table.wait_until_exists()
        except Exception:
            pass  # tabela já existe

        # Mock do orchestrator para retornar findings sintéticos
        mock_findings = [
            {
                "findingId": "f-001",
                "resourceId": "i-001",
                "resourceType": "AWS::EC2::Instance",
                "wasteType": "IDLE",
                "severity": "HIGH",
                "priorityScore": 60.0,
                "recommendation": {
                    "action": "STOP",
                    "estimatedMonthlySavings": 50.0,
                    "estimatedMonthlyCarbonReduction": 0.08,
                    "riskLevel": "MEDIUM",
                },
                "tags": {"GreenOpsManaged": "true"},
                "ttl": 9999999999,
            }
        ]

        with patch.object(dh.DiscoveryOrchestrator, "run_all_scans", return_value=mock_findings):
            with patch("boto3.Session", return_value=moto_session):
                resp = dh.lambda_handler(
                    {"source": "eventbridge-schedule", "action": "full-discovery"},
                    None,
                )

        import json
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["findingsCount"] == 1
        assert body["region"] == REGION
        assert "timestamp" in body

    def test_dry_run_does_not_save_to_dynamodb(self, moto_session):
        """
        Modo dry-run não deve salvar findings no DynamoDB.
        """
        os.environ["FINDINGS_TABLE"] = "greenops-findings-test"

        mock_findings = [{
            "findingId": "f-dry",
            "resourceId": "i-dry",
            "resourceType": "AWS::EC2::Instance",
            "wasteType": "IDLE",
            "severity": "HIGH",
            "priorityScore": 50.0,
            "recommendation": {
                "action": "STOP",
                "estimatedMonthlySavings": 50.0,
                "estimatedMonthlyCarbonReduction": 0.08,
                "riskLevel": "MEDIUM",
            },
            "tags": {},
            "ttl": 9999999999,
        }]

        with patch.object(dh.DiscoveryOrchestrator, "run_all_scans", return_value=mock_findings):
            with patch("boto3.Session", return_value=moto_session):
                resp = dh.lambda_handler(
                    {"source": "test", "action": "dry-run"},
                    None,
                )

        import json
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200
        assert body["dryRun"] is True
        assert body["savedCount"] == 0

    def test_returns_500_on_scan_failure(self, moto_session):
        """
        Falha catastrófica no run_all_scans deve retornar statusCode=500.
        """
        with patch.object(
            dh.DiscoveryOrchestrator,
            "run_all_scans",
            side_effect=RuntimeError("Falha simulada"),
        ):
            with patch("boto3.Session", return_value=moto_session):
                resp = dh.lambda_handler({}, None)

        import json
        assert resp["statusCode"] == 500
        body = json.loads(resp["body"])
        assert "error" in body

    def test_response_includes_by_waste_type(self, moto_session):
        """
        Resposta deve incluir contagem de findings por wasteType.
        """
        os.environ["FINDINGS_TABLE"] = "greenops-findings-test"

        mock_findings = [
            {
                "findingId": f"f-{i}",
                "resourceId": f"i-{i:03d}",
                "resourceType": "AWS::EC2::Instance",
                "wasteType": "IDLE" if i < 2 else "ORPHAN",
                "severity": "HIGH",
                "priorityScore": 50.0,
                "recommendation": {
                    "action": "STOP",
                    "estimatedMonthlySavings": 50.0,
                    "estimatedMonthlyCarbonReduction": 0.08,
                    "riskLevel": "MEDIUM",
                },
                "tags": {},
                "ttl": 9999999999,
            }
            for i in range(3)
        ]

        with patch.object(dh.DiscoveryOrchestrator, "run_all_scans", return_value=mock_findings):
            with patch("boto3.Session", return_value=moto_session):
                resp = dh.lambda_handler({"action": "full-discovery"}, None)

        import json
        body = json.loads(resp["body"])
        assert body["findingsCount"] == 3
        assert body["byWasteType"]["IDLE"] == 2
        assert body["byWasteType"]["ORPHAN"] == 1
