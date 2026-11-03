"""Maps processor names (from config) to Processor classes."""

from __future__ import annotations

from aiproxy.pipeline.base import Pipeline, Processor
from aiproxy.processors.anonymizer import Anonymizer
from aiproxy.processors.image_redactor import ImageRedactor
from aiproxy.processors.logger import Logger
from aiproxy.processors.secrets_regex import SecretsRegex

REGISTRY: dict[str, type[Processor]] = {
    Logger.name: Logger,
    SecretsRegex.name: SecretsRegex,
    Anonymizer.name: Anonymizer,
    ImageRedactor.name: ImageRedactor,
}


def build_pipeline(names: list[str], config: dict) -> Pipeline:
    procs: list[Processor] = []
    for n in names:
        if n not in REGISTRY:
            raise KeyError(f"unknown processor '{n}'; known: {sorted(REGISTRY)}")
        procs.append(REGISTRY[n](config.get(n, {})))
    return Pipeline(procs)
