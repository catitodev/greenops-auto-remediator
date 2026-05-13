"""
GreenOps Auto-Remediador — Utilitários Gerais
=============================================
Funções puras de formatação, cálculo e conversão usadas pelos módulos
discovery, remediation e reporting.

Todas as funções são stateless e não dependem de I/O externo, o que
facilita testes unitários sem mocks.

Uso típico::

    from shared.utils import (
        format_currency,
        format_carbon,
        calculate_priority_score,
        parse_tags,
        estimate_trees_equivalent,
        estimate_cars_equivalent,
    )

    score = calculate_priority_score(
        monthly_savings=150.0,
        carbon_mtco2e=0.8,
        severity_weight=75,   # Severity.HIGH.weight
        confidence=0.9,
    )
    print(format_currency(150.0))       # "$150.00"
    print(format_carbon(0.8))           # "0.800 MTCO2e"
    print(estimate_trees_equivalent(1)) # 45
"""

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# =============================================================================
# Constantes de conversão de carbono
# =============================================================================

# Árvores necessárias para absorver 1 tonelada métrica de CO2 por ano.
# Fonte: EPA — uma árvore adulta absorve ~21,77 kg CO2/ano → ~45,9 árvores/MTCO2e.
# Usamos 45 como valor conservador arredondado.
_TREES_PER_MTCO2E: float = 45.0

# Carros equivalentes removidos por 1 MTCO2e economizado por ano.
# Fonte: EPA — emissão média de um carro de passeio nos EUA: ~4,6 MTCO2e/ano.
# 1 MTCO2e / 4,6 ≈ 0,217 carros.
_CARS_PER_MTCO2E: float = 1.0 / 4.6

# Pesos da fórmula do PriorityScore (devem somar 1.0)
# Ajuste esses pesos para calibrar a importância relativa de cada dimensão.
_WEIGHT_SAVINGS: float = 0.40   # 40% — impacto financeiro
_WEIGHT_CARBON: float = 0.30    # 30% — impacto ambiental
_WEIGHT_SEVERITY: float = 0.20  # 20% — severidade do finding
_WEIGHT_CONFIDENCE: float = 0.10  # 10% — confiança na estimativa

# Valor de referência para normalizar savings (USD/mês)
# Findings com savings >= este valor recebem score máximo na dimensão financeira.
_SAVINGS_NORMALIZATION_USD: float = 500.0

# Valor de referência para normalizar carbono (MTCO2e/mês)
# Findings com carbono >= este valor recebem score máximo na dimensão ambiental.
_CARBON_NORMALIZATION_MTCO2E: float = 5.0


# =============================================================================
# Formatação
# =============================================================================

def format_currency(value: float) -> str:
    """
    Formata um valor monetário em dólares americanos.

    Usa separador de milhar (vírgula) e duas casas decimais.
    Valores negativos são exibidos com sinal de menos antes do símbolo.

    Args:
        value: Valor em USD. Pode ser negativo (ex: custo adicional).

    Returns:
        str: Valor formatado (ex: "$1,234.56", "-$50.00", "$0.00").

    Raises:
        TypeError: Se ``value`` não for numérico.

    Example::

        format_currency(1234.5)    # "$1,234.50"
        format_currency(0)         # "$0.00"
        format_currency(-50.75)    # "-$50.75"
        format_currency(1_000_000) # "$1,000,000.00"
    """
    if not isinstance(value, (int, float)):
        raise TypeError(f"value deve ser numérico, recebido: {type(value).__name__}")

    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"value não pode ser NaN ou infinito: {value}")

    abs_value = abs(value)
    sign = "-" if value < 0 else ""

    # Formata com separador de milhar e 2 casas decimais
    formatted = f"{abs_value:,.2f}"
    return f"{sign}${formatted}"


def format_carbon(value: float) -> str:
    """
    Formata um valor de emissão de carbono em toneladas métricas de CO2 equivalente.

    Usa três casas decimais para precisão em valores pequenos (< 1 MTCO2e),
    que são comuns em recursos individuais de cloud.

    Args:
        value: Emissão em MTCO2e (toneladas métricas de CO2 equivalente).
               Deve ser >= 0.

    Returns:
        str: Valor formatado (ex: "0.800 MTCO2e", "1.234 MTCO2e", "12.500 MTCO2e").

    Raises:
        TypeError: Se ``value`` não for numérico.
        ValueError: Se ``value`` for negativo, NaN ou infinito.

    Example::

        format_carbon(0.8)    # "0.800 MTCO2e"
        format_carbon(1.234)  # "1.234 MTCO2e"
        format_carbon(12.5)   # "12.500 MTCO2e"
        format_carbon(0)      # "0.000 MTCO2e"
    """
    if not isinstance(value, (int, float)):
        raise TypeError(f"value deve ser numérico, recebido: {type(value).__name__}")

    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"value não pode ser NaN ou infinito: {value}")

    if value < 0:
        raise ValueError(f"Emissão de carbono não pode ser negativa: {value}")

    return f"{value:.3f} MTCO2e"


# =============================================================================
# Cálculo de prioridade
# =============================================================================

def calculate_priority_score(
    monthly_savings: float,
    carbon_mtco2e: float,
    severity_weight: int,
    confidence: float,
) -> float:
    """
    Calcula o PriorityScore de um finding para ordenação e triagem.

    O score combina quatro dimensões em um valor de 0.0 a 100.0:

    .. code-block:: text

        PriorityScore = (
            (savings_norm  * 0.40) +
            (carbon_norm   * 0.30) +
            (severity_norm * 0.20) +
            (confidence    * 0.10)
        ) * 100

    Onde cada dimensão é normalizada para [0.0, 1.0]:

    - ``savings_norm``  = min(monthly_savings / 500, 1.0)
    - ``carbon_norm``   = min(carbon_mtco2e  / 5.0,  1.0)
    - ``severity_norm`` = severity_weight / 100  (já em escala 0–100)
    - ``confidence``    = clamp(confidence, 0.0, 1.0)

    Os pesos (0.40, 0.30, 0.20, 0.10) refletem a prioridade do produto:
    impacto financeiro > impacto ambiental > severidade > confiança.

    Args:
        monthly_savings: Economia mensal estimada em USD. Deve ser >= 0.
        carbon_mtco2e: Redução de emissão estimada em MTCO2e/mês. Deve ser >= 0.
        severity_weight: Peso numérico da severidade (25, 50, 75 ou 100).
            Use ``Severity.HIGH.weight`` para obter o valor correto.
        confidence: Confiança na estimativa, entre 0.0 e 1.0.
            Exemplos: 0.9 = alta confiança (dados CloudWatch), 0.5 = estimativa.

    Returns:
        float: Score de prioridade entre 0.0 e 100.0, arredondado a 2 casas.
            Scores mais altos indicam findings que devem ser remediados primeiro.

    Raises:
        ValueError: Se algum argumento estiver fora do intervalo válido.

    Example::

        # Finding crítico: $300/mês, 2 MTCO2e, severidade HIGH (75), 90% confiança
        score = calculate_priority_score(300.0, 2.0, 75, 0.9)
        # savings_norm  = 300/500 = 0.60
        # carbon_norm   = 2.0/5.0 = 0.40
        # severity_norm = 75/100  = 0.75
        # confidence    = 0.90
        # score = (0.60*0.40 + 0.40*0.30 + 0.75*0.20 + 0.90*0.10) * 100
        #       = (0.24 + 0.12 + 0.15 + 0.09) * 100 = 60.0

        # Finding menor: $10/mês, 0.1 MTCO2e, severidade LOW (25), 50% confiança
        score = calculate_priority_score(10.0, 0.1, 25, 0.5)
        # score ≈ 10.1
    """
    # Validações de entrada
    if monthly_savings < 0:
        raise ValueError(f"monthly_savings não pode ser negativo: {monthly_savings}")
    if carbon_mtco2e < 0:
        raise ValueError(f"carbon_mtco2e não pode ser negativo: {carbon_mtco2e}")
    if severity_weight not in (25, 50, 75, 100):
        raise ValueError(
            f"severity_weight deve ser 25, 50, 75 ou 100 (use Severity.<level>.weight). "
            f"Recebido: {severity_weight}"
        )
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"confidence deve estar entre 0.0 e 1.0: {confidence}")

    # Normalização de cada dimensão para [0.0, 1.0]
    savings_norm: float = min(monthly_savings / _SAVINGS_NORMALIZATION_USD, 1.0)
    carbon_norm: float = min(carbon_mtco2e / _CARBON_NORMALIZATION_MTCO2E, 1.0)
    severity_norm: float = severity_weight / 100.0
    # confidence já está em [0.0, 1.0]

    # Fórmula ponderada
    raw_score: float = (
        (savings_norm * _WEIGHT_SAVINGS)
        + (carbon_norm * _WEIGHT_CARBON)
        + (severity_norm * _WEIGHT_SEVERITY)
        + (confidence * _WEIGHT_CONFIDENCE)
    )

    # Converte para escala 0–100 e arredonda
    score = round(raw_score * 100.0, 2)

    logger.debug(
        "PriorityScore calculado: %.2f "
        "(savings_norm=%.3f, carbon_norm=%.3f, severity_norm=%.3f, confidence=%.3f)",
        score, savings_norm, carbon_norm, severity_norm, confidence,
    )

    return score


# =============================================================================
# Conversão de tags AWS
# =============================================================================

def parse_tags(tag_list: list[dict[str, str]]) -> dict[str, str]:
    """
    Converte a lista de tags no formato nativo da API AWS para um dict Python.

    A API AWS retorna tags como lista de objetos ``{"Key": "...", "Value": "..."}``
    em chamadas como ``ec2.describe_instances``, ``rds.describe_db_instances``, etc.
    Esta função normaliza esse formato para um dict simples ``{chave: valor}``.

    Entradas inválidas (itens sem "Key" ou "Value") são ignoradas com log de aviso.

    Args:
        tag_list: Lista de dicts no formato AWS.
            Exemplo: ``[{"Key": "Env", "Value": "prod"}, {"Key": "Team", "Value": "ops"}]``
            Aceita lista vazia → retorna dict vazio.
            Aceita None → retorna dict vazio com aviso.

    Returns:
        dict[str, str]: Tags como ``{"Env": "prod", "Team": "ops"}``.

    Example::

        aws_tags = [
            {"Key": "GreenOpsManaged", "Value": "true"},
            {"Key": "Environment",     "Value": "dev"},
        ]
        parse_tags(aws_tags)
        # {"GreenOpsManaged": "true", "Environment": "dev"}

        parse_tags([])   # {}
        parse_tags(None) # {}  (com aviso no log)
    """
    if tag_list is None:
        logger.warning("parse_tags recebeu None — retornando dict vazio")
        return {}

    if not isinstance(tag_list, list):
        logger.warning(
            "parse_tags esperava list, recebeu %s — retornando dict vazio",
            type(tag_list).__name__,
        )
        return {}

    result: dict[str, str] = {}

    for i, item in enumerate(tag_list):
        if not isinstance(item, dict):
            logger.warning("parse_tags: item[%d] não é dict (%s) — ignorado", i, type(item).__name__)
            continue

        key = item.get("Key")
        value = item.get("Value")

        if key is None or value is None:
            logger.warning(
                "parse_tags: item[%d] sem 'Key' ou 'Value' (%r) — ignorado", i, item
            )
            continue

        result[str(key)] = str(value)

    return result


# =============================================================================
# Equivalências de carbono
# =============================================================================

def estimate_trees_equivalent(mtco2e: float) -> int:
    """
    Estima quantas árvores precisariam ser plantadas para absorver a emissão
    equivalente em um ano.

    Usa o fator de absorção da EPA: uma árvore adulta absorve ~21,77 kg CO2/ano,
    portanto 1 MTCO2e ≈ 45 árvores. Este número é usado em relatórios de
    sustentabilidade para tornar o impacto ambiental tangível para stakeholders.

    Args:
        mtco2e: Emissão em toneladas métricas de CO2 equivalente. Deve ser >= 0.

    Returns:
        int: Número de árvores equivalentes (arredondado para cima).
            Retorna 0 para entradas zero ou muito pequenas.

    Raises:
        ValueError: Se ``mtco2e`` for negativo, NaN ou infinito.

    Example::

        estimate_trees_equivalent(1.0)   # 45
        estimate_trees_equivalent(0.5)   # 23  (ceil de 22.5)
        estimate_trees_equivalent(10.0)  # 450
        estimate_trees_equivalent(0.0)   # 0
    """
    _validate_carbon_value(mtco2e, "mtco2e")

    if mtco2e == 0.0:
        return 0

    # math.ceil garante que mesmo frações pequenas resultem em pelo menos 1 árvore
    return math.ceil(mtco2e * _TREES_PER_MTCO2E)


def estimate_cars_equivalent(mtco2e: float) -> float:
    """
    Estima quantos carros de passeio seriam removidos das ruas por um ano
    para compensar a emissão equivalente.

    Usa o fator da EPA: emissão média de um carro de passeio nos EUA ≈ 4,6 MTCO2e/ano.
    Portanto 1 MTCO2e ≈ 0,217 carros removidos.

    Valores menores que 1 carro são retornados como fração (ex: 0.22),
    pois são úteis para somar múltiplos findings em relatórios agregados.

    Args:
        mtco2e: Emissão em toneladas métricas de CO2 equivalente. Deve ser >= 0.

    Returns:
        float: Número de carros equivalentes, arredondado a 2 casas decimais.
            Retorna 0.0 para entradas zero.

    Raises:
        ValueError: Se ``mtco2e`` for negativo, NaN ou infinito.

    Example::

        estimate_cars_equivalent(4.6)   # 1.0
        estimate_cars_equivalent(1.0)   # 0.22
        estimate_cars_equivalent(46.0)  # 10.0
        estimate_cars_equivalent(0.0)   # 0.0
    """
    _validate_carbon_value(mtco2e, "mtco2e")

    if mtco2e == 0.0:
        return 0.0

    return round(mtco2e * _CARS_PER_MTCO2E, 2)


# =============================================================================
# Helpers internos
# =============================================================================

def _validate_carbon_value(value: Any, param_name: str) -> None:
    """
    Valida que um valor de carbono é numérico, finito e não-negativo.

    Args:
        value: Valor a validar.
        param_name: Nome do parâmetro para mensagens de erro.

    Raises:
        TypeError: Se ``value`` não for numérico.
        ValueError: Se ``value`` for negativo, NaN ou infinito.
    """
    if not isinstance(value, (int, float)):
        raise TypeError(
            f"{param_name} deve ser numérico, recebido: {type(value).__name__}"
        )
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"{param_name} não pode ser NaN ou infinito: {value}")
    if value < 0:
        raise ValueError(f"{param_name} não pode ser negativo: {value}")
