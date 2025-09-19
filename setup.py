from setuptools import setup, find_packages

setup(
    name="source-pm",
    version="0.1.0",
    description="Gerenciador de pacotes Linux baseado em recipes.",
    author="Seu Nome",
    license="GPL-3.0",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "rich>=13.0.0",
    ],
    entry_points={
        "console_scripts": [
            "source=source.source:main",
        ],
    },
)
