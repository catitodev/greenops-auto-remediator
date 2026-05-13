"""
GreenOps Auto-Remediador — Configuração do Sistema
===================================================
Carrega e valida variáveis de ambiente, expõe helpers para construir
nomes de recursos AWS e verificar tags obrigatórias em recursos.

Uso típico::

    from shared.config import load_config, get_table_name, validate_required_tags

    cfg = load_config()
    table = get_table_name("findings")          # "greenops-findings-dev"
    topic = get_topic_name("notifications")     # "greenops-notifications-dev"

    tags = {"GreenOpsManaged": "true", "Team": "platform"}
    ok, missing = validate_required_tags(tags)
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# =============================================================================
# Constantes internas
# =============================================================================

# Prefixo padrão para todos os recursos AWS do projeto
_RESOURCE_PREFIX = "greenops"

# Ambientes válidos
_VALID_ENVIRONMENTS = {"dev", "staging", "prod"}


# =============================================================================
# Dataclass de configuração
# =============================================================================

@dataclass(frozen=True)
class GreenOpsConfig:
    """
    Configuração imutável do sistema carregada a partir de variáveis de ambiente.

    Todos os campos têm valores padrão seguros para execução local/dev.
    Em produção, as variáveis são injetadas pelo CloudFormation via Lambda
    environment variables.

    Attributes:
        aws_region: Região AWS onde os recursos são gerenciados.
        environment: Ambiente de execução (dev | staging | prod).
        dry_run: Se True, nenhuma ação destrutiva é aplicada — apenas simulada.
        approver_email: Email para notificações e aprovações de remediação.
        slack_webhook_url: Webhook Slack para alertas em tempo real (opcional).
        discovery_schedule: Expressão EventBridge para o schedule de discovery.
        reporting_schedule: Expressão EventBridge para o schedule de reporting.
        max_actions_per_hour: Limite de ações de remediação por hora.
        max_destructive_actions_per_day: Limite de ações destrutivas por dia.
        required_tags: Dict de tags obrigatórias para elegibilidade à remediação.
        protection_tag: Nome da tag que protege um recurso de ações automáticas.
        protection_values: Conjunto de valores da protection_tag que bloqueiam ações.
        findings_table: Nome completo da tabela DynamoDB de findings.
        rollbacks_table: Nome completo da tabela DynamoDB de rollbacks.
        approvals_table: Nome completo da tabela DynamoDB de aprovações.
        config_table: Nome completo da tabela DynamoDB de configuração.
        reports_bucket: Nome do bucket S3 para relatórios.
        notifications_topic_arn: ARN do tópico SNS de notificações.
        approvals_topic_arn: ARN do tópico SNS de aprovações.
        alerts_topic_arn: ARN do tópico SNS de alertas críticos.
    """

    # AWS
    aws_region: str = "us-east-1"
    environment: str = "dev"

    # Comportamento
    dry_run: bool = True  # padrão seguro: nunca executar sem confirmação explícita

    # Notificações
    approver_email: str = ""
    slack_webhook_url: str = ""

    # Agendamentos
    discovery_schedule: str = "rate(6 hours)"
    reporting_schedule: str = "cron(0 8 * * ? *)"

    # Limites de segurança (blast radius)
    max_actions_per_hour: int = 10
    max_destructive_actions_per_day: int = 5

    # Tags de controle
    required_tags: dict[str, str] = field(default_factory=lambda: {"GreenOpsManaged": "true"})
    protection_tag: str = "GreenOpsProtected"
    protection_values: frozenset[str] = field(default_factory=lambda: frozenset({"true", "yes", "1"}))

    # Nomes de recursos AWS (preenchidos automaticamente por load_config)
    findings_table: str = ""
    rollbacks_table: str = ""
    approvals_table: str = ""
    config_table: str = ""
    reports_bucket: str = ""
    notifications_topic_arn: str = ""
    approvals_topic_arn: str = ""
    alerts_topic_arn: str = ""

    @property
    def is_production(self) -> bool:
        """Retorna True se o ambiente for produção."""
        return self.environment == "prod"

    @property
    def is_dry_run(self) -> bool:
        """Alias legível para dry_run — sempre True em produção por segurança extra."""
        return self.dry_run or self.is_production


# =============================================================================
# Funções auxiliares de parsing
# =============================================================================

def _parse_bool(value: str, default: bool = True) -> bool:
    """
    Converte string de variável de ambiente para bool de forma segura.

    Aceita: "true", "1", "yes" → True; qualquer outro valor → False.
    O padrão é True (seguro) para evitar execuções acidentais.

    Args:
        value: String lida do ambiente.
        default: Valor retornado se ``value`` for vazio ou None.

    Returns:
        bool correspondente ao valor da string.
    """
    if not value:
        return default
    return value.strip().lower() in {"true", "1", "yes"}


def _parse_int(value: str, default: int, min_value: int = 0) -> int:
    """
    Converte string de variável de ambiente para int com fallback seguro.

    Args:
        value: String lida do ambiente.
        default: Valor retornado se ``value`` for inválido ou vazio.
        min_value: Valor mínimo aceito; valores abaixo são substituídos pelo default.

    Returns:
        int correspondente ao valor da string, ou ``default`` em caso de erro.
    """
    try:
        parsed = int(value.strip())
        return parsed if parsed >= min_value else default
    except (ValueError, AttributeError):
        logger.warning("Valor inválido para variável inteira: %r — usando padrão %d", value, default)
        return default


def _parse_json_dict(value: str, default: dict[str, str] | None = None) -> dict[str, str]:
    """
    Faz parse de uma string JSON para dict de forma segura.

    Usado para REQUIRED_TAGS, que é armazenado como JSON no ambiente.

    Args:
        value: String JSON lida do ambiente (ex: '{"GreenOpsManaged": "true"}').
        default: Dict retornado se ``value`` for inválido ou vazio.

    Returns:
        dict[str, str] com os pares chave-valor parseados.
    """
    if default is None:
        default = {"GreenOpsManaged": "true"}
    if not value:
        return default
    try:
        parsed = json.loads(value.strip())
        if not isinstance(parsed, dict):
            raise ValueError("REQUIRED_TAGS deve ser um objeto JSON, não uma lista ou valor simples")
        return {str(k): str(v) for k, v in parsed.items()}
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Falha ao parsear REQUIRED_TAGS: %s — usando padrão %r", exc, default)
        return default


def _parse_csv_set(value: str, default: frozenset[str] | None = None) -> frozenset[str]:
    """
    Converte string CSV para frozenset de strings normalizadas (lowercase, sem espaços).

    Usado para PROTECTION_VALUES.

    Args:
        value: String CSV lida do ambiente (ex: "true,yes,1").
        default: frozenset retornado se ``value`` for vazio.

    Returns:
        frozenset[str] com os valores normalizados.
    """
    if default is None:
        default = frozenset({"true", "yes", "1"})
    if not value:
        return default
    return frozenset(v.strip().lower() for v in value.split(",") if v.strip())


# =============================================================================
# Funções públicas
# =============================================================================

def load_config() -> GreenOpsConfig:
    """
    Carrega todas as variáveis de ambiente e retorna um ``GreenOpsConfig`` imutável.

    Variáveis lidas do ambiente (com valores padrão seguros):

    +------------------------------+------------------+---------------------------+
    | Variável                     | Padrão           | Descrição                 |
    +==============================+==================+===========================+
    | AWS_REGION                   | us-east-1        | Região AWS                |
    | GREENOPS_ENVIRONMENT         | dev              | Ambiente de execução      |
    | GREENOPS_DRY_RUN             | true             | Modo simulação            |
    | APPROVER_EMAIL               | ""               | Email de aprovação        |
    | SLACK_WEBHOOK_URL            | ""               | Webhook Slack (opcional)  |
    | DISCOVERY_SCHEDULE           | rate(6 hours)    | Schedule EventBridge      |
    | REPORTING_SCHEDULE           | cron(0 8 * * ? *)| Schedule EventBridge      |
    | MAX_ACTIONS_PER_HOUR         | 10               | Limite de ações/hora      |
    | MAX_DESTRUCTIVE_ACTIONS_PER_DAY | 5             | Limite destrutivas/dia    |
    | REQUIRED_TAGS                | {"GreenOpsManaged": "true"} | Tags JSON    |
    | PROTECTION_TAG               | GreenOpsProtected| Tag de proteção           |
    | PROTECTION_VALUES            | true,yes,1       | Valores de proteção (CSV) |
    | FINDINGS_TABLE               | (gerado)         | Nome tabela DynamoDB      |
    | ROLLBACKS_TABLE              | (gerado)         | Nome tabela DynamoDB      |
    | APPROVALS_TABLE              | (gerado)         | Nome tabela DynamoDB      |
    | CONFIG_TABLE                 | (gerado)         | Nome tabela DynamoDB      |
    | REPORTS_BUCKET               | ""               | Nome bucket S3            |
    | NOTIFICATIONS_TOPIC_ARN      | ""               | ARN tópico SNS            |
    | APPROVALS_TOPIC_ARN          | ""               | ARN tópico SNS            |
    | ALERTS_TOPIC_ARN             | ""               | ARN tópico SNS            |
    +------------------------------+------------------+---------------------------+

    Returns:
        GreenOpsConfig: Objeto de configuração imutável (frozen dataclass).

    Raises:
        ValueError: Se GREENOPS_ENVIRONMENT contiver um valor inválido.

    Example::

        cfg = load_config()
        print(cfg.environment)       # "dev"
        print(cfg.dry_run)           # True
        print(cfg.required_tags)     # {"GreenOpsManaged": "true"}
    """
    environment = os.getenv("GREENOPS_ENVIRONMENT", "dev").strip().lower()

    if environment not in _VALID_ENVIRONMENTS:
        raise ValueError(
            f"GREENOPS_ENVIRONMENT inválido: {environment!r}. "
            f"Valores aceitos: {sorted(_VALID_ENVIRONMENTS)}"
        )

    # Nomes de tabelas: lê do ambiente (injetado pelo CloudFormation) ou gera
    findings_table = (
        os.getenv("FINDINGS_TABLE") or get_table_name("findings", environment)
    )
    rollbacks_table = (
        os.getenv("ROLLBACKS_TABLE") or get_table_name("rollbacks", environment)
    )
    approvals_table = (
        os.getenv("APPROVALS_TABLE") or get_table_name("approvals", environment)
    )
    config_table = (
        os.getenv("CONFIG_TABLE") or get_table_name("config", environment)
    )

    return GreenOpsConfig(
        # AWS
        aws_region=os.getenv("AWS_REGION", "us-east-1").strip(),
        environment=environment,

        # Comportamento — padrão True (seguro)
        dry_run=_parse_bool(os.getenv("GREENOPS_DRY_RUN", "true"), default=True),

        # Notificações
        approver_email=os.getenv("APPROVER_EMAIL", "").strip(),
        slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", "").strip(),

        # Agendamentos
        discovery_schedule=os.getenv("DISCOVERY_SCHEDULE", "rate(6 hours)").strip(),
        reporting_schedule=os.getenv("REPORTING_SCHEDULE", "cron(0 8 * * ? *)").strip(),

        # Limites de segurança
        max_actions_per_hour=_parse_int(
            os.getenv("MAX_ACTIONS_PER_HOUR", "10"), default=10, min_value=1
        ),
        max_destructive_actions_per_day=_parse_int(
            os.getenv("MAX_DESTRUCTIVE_ACTIONS_PER_DAY", "5"), default=5, min_value=1
        ),

        # Tags de controle
        required_tags=_parse_json_dict(
            os.getenv("REQUIRED_TAGS", '{"GreenOpsManaged": "true"}')
        ),
        protection_tag=os.getenv("PROTECTION_TAG", "GreenOpsProtected").strip(),
        protection_values=_parse_csv_set(
            os.getenv("PROTECTION_VALUES", "true,yes,1")
        ),

        # Nomes de recursos AWS
        findings_table=findings_table,
        rollbacks_table=rollbacks_table,
        approvals_table=approvals_table,
        config_table=config_table,
        reports_bucket=os.getenv("REPORTS_BUCKET", "").strip(),
        notifications_topic_arn=os.getenv("NOTIFICATIONS_TOPIC_ARN", "").strip(),
        approvals_topic_arn=os.getenv("APPROVALS_TOPIC_ARN", "").strip(),
        alerts_topic_arn=os.getenv("ALERTS_TOPIC_ARN", "").strip(),
    )


def get_table_name(suffix: str, environment: str | None = None) -> str:
    """
    Retorna o nome padronizado de uma tabela DynamoDB do GreenOps.

    O padrão é: ``greenops-{suffix}-{environment}``

    Se ``environment`` não for fornecido, lê de GREENOPS_ENVIRONMENT
    (padrão: "dev").

    Args:
        suffix: Identificador da tabela (ex: "findings", "rollbacks").
        environment: Ambiente de execução. Se None, lê da variável de ambiente.

    Returns:
        str: Nome completo da tabela (ex: "greenops-findings-dev").

    Raises:
        ValueError: Se ``suffix`` for vazio ou contiver caracteres inválidos.

    Example::

        get_table_name("findings")           # "greenops-findings-dev"
        get_table_name("rollbacks", "prod")  # "greenops-rollbacks-prod"
    """
    if not suffix or not suffix.strip():
        raise ValueError("suffix não pode ser vazio")

    # Normaliza: lowercase, sem espaços
    clean_suffix = suffix.strip().lower()

    env = (environment or os.getenv("GREENOPS_ENVIRONMENT", "dev")).strip().lower()

    return f"{_RESOURCE_PREFIX}-{clean_suffix}-{env}"


def get_topic_name(suffix: str, environment: str | None = None) -> str:
    """
    Retorna o nome padronizado de um tópico SNS do GreenOps.

    O padrão é: ``greenops-{suffix}-{environment}``

    Se ``environment`` não for fornecido, lê de GREENOPS_ENVIRONMENT
    (padrão: "dev").

    Args:
        suffix: Identificador do tópico (ex: "notifications", "approvals", "alerts").
        environment: Ambiente de execução. Se None, lê da variável de ambiente.

    Returns:
        str: Nome completo do tópico (ex: "greenops-notifications-dev").

    Raises:
        ValueError: Se ``suffix`` for vazio.

    Example::

        get_topic_name("notifications")          # "greenops-notifications-dev"
        get_topic_name("approvals", "staging")   # "greenops-approvals-staging"
    """
    if not suffix or not suffix.strip():
        raise ValueError("suffix não pode ser vazio")

    clean_suffix = suffix.strip().lower()
    env = (environment or os.getenv("GREENOPS_ENVIRONMENT", "dev")).strip().lower()

    return f"{_RESOURCE_PREFIX}-{clean_suffix}-{env}"


def validate_required_tags(
    tags_dict: dict[str, Any],
    required_tags: dict[str, str] | None = None,
) -> tuple[bool, list[str]]:
    """
    Verifica se um recurso AWS possui todas as tags obrigatórias com os valores corretos.

    Compara as tags do recurso com ``required_tags``. A comparação de valores
    é case-insensitive para tolerar variações como "True" vs "true".

    Args:
        tags_dict: Tags do recurso AWS no formato ``{"chave": "valor"}``.
            Aceita também o formato nativo da API AWS:
            ``[{"Key": "chave", "Value": "valor"}]`` — convertido automaticamente.
        required_tags: Dict de tags obrigatórias a verificar. Se None, carrega
            de REQUIRED_TAGS via ``load_config()``.

    Returns:
        tuple[bool, list[str]]: Par (válido, lista_de_tags_faltando).
            - válido: True se todas as tags obrigatórias estão presentes e corretas.
            - lista_de_tags_faltando: Tags ausentes ou com valor incorreto.
              Vazia quando válido=True.

    Example::

        tags = {"GreenOpsManaged": "true", "Environment": "dev"}
        ok, missing = validate_required_tags(tags)
        # ok=True, missing=[]

        tags = {"Environment": "dev"}
        ok, missing = validate_required_tags(tags)
        # ok=False, missing=["GreenOpsManaged"]

        # Formato nativo da API AWS também é aceito:
        tags = [{"Key": "GreenOpsManaged", "Value": "true"}]
        ok, missing = validate_required_tags(tags)
        # ok=True, missing=[]
    """
    # Normaliza formato de lista AWS → dict
    normalized: dict[str, str] = {}
    if isinstance(tags_dict, list):
        for item in tags_dict:
            if isinstance(item, dict) and "Key" in item and "Value" in item:
                normalized[item["Key"]] = str(item["Value"])
    elif isinstance(tags_dict, dict):
        normalized = {str(k): str(v) for k, v in tags_dict.items()}
    else:
        logger.warning("tags_dict com tipo inesperado: %s", type(tags_dict))
        return False, ["<formato inválido>"]

    # Carrega required_tags do ambiente se não fornecido
    if required_tags is None:
        required_tags = _parse_json_dict(
            os.getenv("REQUIRED_TAGS", '{"GreenOpsManaged": "true"}')
        )

    missing: list[str] = []

    for required_key, required_value in required_tags.items():
        resource_value = normalized.get(required_key)

        if resource_value is None:
            # Tag completamente ausente
            missing.append(required_key)
            logger.debug("Tag obrigatória ausente: %r", required_key)
        elif resource_value.strip().lower() != required_value.strip().lower():
            # Tag presente mas com valor incorreto
            missing.append(required_key)
            logger.debug(
                "Tag %r com valor incorreto: esperado %r, encontrado %r",
                required_key,
                required_value,
                resource_value,
            )

    is_valid = len(missing) == 0
    return is_valid, missing
