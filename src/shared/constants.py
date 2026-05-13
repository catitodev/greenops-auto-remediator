"""
GreenOps Auto-Remediador — Constantes e Enumerações
====================================================
Define os tipos centrais usados em todo o sistema para classificar
recursos desperdiçados, severidade de findings, ações de remediação
e níveis de risco associados a cada operação.

Todos os enums herdam de ``str`` para permitir:
- Serialização direta em JSON sem ``.value``
- Comparação com strings vindas do DynamoDB ou de eventos EventBridge
- Uso como chave em dicionários sem conversão explícita
"""

from enum import Enum


# =============================================================================
# WasteType — Tipo de desperdício identificado
# =============================================================================

class WasteType(str, Enum):
    """
    Classifica o tipo de desperdício identificado em um recurso AWS.

    Usado pelo módulo ``discovery`` ao registrar um finding na tabela
    DynamoDB e pelo módulo ``reporting`` para agrupar métricas por categoria.

    Exemplos por tipo:
    - IDLE: EC2 com CPU < 5% por 14 dias, RDS sem conexões ativas
    - ORPHAN: EBS volume sem instância, Elastic IP não associado
    - OVERSIZED: m5.4xlarge com uso médio de 8% de CPU
    - MISCONFIGURED: S3 sem lifecycle policy, Lambda com memória excessiva
    """

    # Recurso provisionado mas sem uso mensurável por período prolongado
    IDLE = "IDLE"

    # Recurso desanexado ou sem vínculo com outros recursos ativos
    ORPHAN = "ORPHAN"

    # Recurso superdimensionado para a carga de trabalho real
    OVERSIZED = "OVERSIZED"

    # Recurso com configuração subótima que gera custo ou emissão desnecessária
    MISCONFIGURED = "MISCONFIGURED"


# =============================================================================
# Severity — Severidade de um finding
# =============================================================================

class Severity(str, Enum):
    """
    Indica a severidade de um finding — combinação de impacto financeiro
    e de carbono com a urgência de remediação.

    Cada membro carrega um ``weight`` numérico (0–100) usado para:
    - Calcular scores agregados de desperdício por conta/região
    - Ordenar findings em dashboards e relatórios
    - Definir thresholds de alerta no CloudWatch

    Ordem crescente de impacto: LOW < MEDIUM < HIGH < CRITICAL

    Uso::

        score = sum(f.severity.weight for f in findings) / len(findings)
        if score > Severity.HIGH.weight:
            send_alert()
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def weight(self) -> int:
        """
        Peso numérico da severidade para cálculo de scores agregados.

        Returns:
            int: 25 (LOW), 50 (MEDIUM), 75 (HIGH), 100 (CRITICAL)
        """
        _weights = {
            "LOW": 25,
            "MEDIUM": 50,
            "HIGH": 75,
            "CRITICAL": 100,
        }
        return _weights[self.value]

    def __lt__(self, other: "Severity") -> bool:
        """Permite comparação ordinal: Severity.LOW < Severity.HIGH."""
        if not isinstance(other, Severity):
            return NotImplemented
        return self.weight < other.weight

    def __le__(self, other: "Severity") -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.weight <= other.weight

    def __gt__(self, other: "Severity") -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.weight > other.weight

    def __ge__(self, other: "Severity") -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.weight >= other.weight


# =============================================================================
# ActionType — Ações de remediação disponíveis
# =============================================================================

class ActionType(str, Enum):
    """
    Define as ações de remediação que o sistema pode executar em recursos AWS.

    Cada ação tem um ``RiskLevel`` associado (ver tabela abaixo) que determina
    o fluxo de aprovação antes da execução.

    +------------------+------------------+----------------------------------+
    | Ação             | Risco padrão     | Recursos alvo                    |
    +==================+==================+==================================+
    | STOP             | MEDIUM           | EC2, RDS, ECS tasks              |
    | START            | LOW              | EC2, RDS                         |
    | RESIZE           | HIGH             | EC2, RDS, Lambda                 |
    | DELETE           | CRITICAL         | EBS, snapshots, NAT GW           |
    | RELEASE          | HIGH             | Elastic IPs                      |
    | TAG              | LOW              | Qualquer recurso taggeável       |
    | APPLY_LIFECYCLE  | LOW              | S3 buckets, DynamoDB tables      |
    +------------------+------------------+----------------------------------+

    Nota: o risco efetivo pode ser elevado pelo módulo ``remediation``
    dependendo do ambiente (prod sempre eleva em um nível) e do valor
    financeiro do recurso.
    """

    # Para uma instância ou serviço em execução (reversível via START)
    STOP = "STOP"

    # Inicia uma instância ou serviço parado
    START = "START"

    # Altera o tipo/tamanho do recurso para um menor (rightsizing)
    RESIZE = "RESIZE"

    # Remove permanentemente o recurso — ação destrutiva e irreversível
    DELETE = "DELETE"

    # Libera um recurso alocado mas não utilizado (ex: Elastic IP)
    RELEASE = "RELEASE"

    # Adiciona ou atualiza tags em um recurso para rastreamento
    TAG = "TAG"

    # Aplica ou atualiza política de lifecycle em S3 buckets ou tabelas DynamoDB
    # Exemplo: mover objetos para Glacier após 90 dias, expirar versões antigas
    APPLY_LIFECYCLE = "APPLY_LIFECYCLE"


# =============================================================================
# RiskLevel — Nível de risco e fluxo de aprovação
# =============================================================================

class RiskLevel(str, Enum):
    """
    Avalia o risco operacional de executar uma ação de remediação e define
    o fluxo de aprovação correspondente.

    Fluxos de aprovação por nível:

    LOW — Execução automática
        Ação totalmente reversível, sem impacto em disponibilidade.
        O sistema executa sem notificação prévia.
        Exemplos: TAG, APPLY_LIFECYCLE, START

    MEDIUM — Aprovação simples obrigatória
        Ação reversível com impacto mínimo e janela de rollback clara.
        Requer aprovação de um responsável via SNS/email antes de executar.
        Exemplos: STOP de instância idle, RESIZE de Lambda

    HIGH — Aprovação + dry-run obrigatório
        Ação com impacto potencial em disponibilidade ou custo de rollback.
        O sistema executa primeiro em modo dry-run, envia o relatório ao
        aprovador e aguarda confirmação explícita para aplicar de verdade.
        Exemplos: RESIZE de EC2/RDS, RELEASE de Elastic IP

    CRITICAL — Aprovação dupla obrigatória
        Ação destrutiva ou irreversível. Requer aprovação independente de
        dois responsáveis diferentes antes de qualquer execução.
        Nunca executado automaticamente, independente do ambiente.
        Exemplos: DELETE de volume, DELETE de snapshot, DELETE de NAT Gateway

    Ordem crescente de restrição: LOW < MEDIUM < HIGH < CRITICAL

    Uso::

        if action.risk_level == RiskLevel.CRITICAL:
            request_dual_approval(action)
        elif action.risk_level == RiskLevel.HIGH:
            run_dry_run_then_request_approval(action)
        elif action.risk_level == RiskLevel.MEDIUM:
            request_single_approval(action)
        else:
            execute_automatically(action)
    """

    # Execução automática permitida — sem aprovação necessária
    LOW = "LOW"

    # Aprovação de um responsável obrigatória antes de executar
    MEDIUM = "MEDIUM"

    # Aprovação obrigatória + dry-run executado previamente para validação
    HIGH = "HIGH"

    # Aprovação dupla (dois aprovadores independentes) obrigatória
    CRITICAL = "CRITICAL"

    @property
    def requires_approval(self) -> bool:
        """
        Indica se o nível de risco exige pelo menos uma aprovação humana.

        Returns:
            bool: True para MEDIUM, HIGH e CRITICAL; False para LOW.
        """
        return self in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)

    @property
    def requires_dry_run(self) -> bool:
        """
        Indica se o nível de risco exige execução prévia em modo dry-run.

        Returns:
            bool: True para HIGH e CRITICAL; False para LOW e MEDIUM.
        """
        return self in (RiskLevel.HIGH, RiskLevel.CRITICAL)

    @property
    def requires_dual_approval(self) -> bool:
        """
        Indica se o nível de risco exige aprovação de dois responsáveis distintos.

        Returns:
            bool: True apenas para CRITICAL.
        """
        return self == RiskLevel.CRITICAL
