"""Seeded synthetic hospitality data generation.

`spec` holds every parameter and assumption; `builder` produces the frames;
`load` writes them to the mart. Same seed in, same dataset out.
"""

from staypulse.generate.builder import Generator, dataset_fingerprint

__all__ = ["Generator", "dataset_fingerprint"]
