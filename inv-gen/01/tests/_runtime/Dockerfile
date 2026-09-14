# syntax=docker/dockerfile:1
FROM ubuntu:24.04 AS ocaml-builder

ARG DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Build-only dependencies stay out of the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        graphviz \
        libgmp-dev \
        libgtksourceview-3.0-dev \
        m4 \
        opam \
        pkg-config \
        rsync \
        unzip \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN opam init --disable-sandboxing --bare --yes \
    && opam switch create col2inv-ocaml ocaml-base-compiler.4.14.2 --yes \
    && opam install --switch=col2inv-ocaml --assume-depexts --yes \
        frama-c.31.0 \
        why3.1.8.1 \
        alt-ergo.2.6.3 \
        z3.4.13.3 \
        coq.8.18.0 \
    && opam exec --switch=col2inv-ocaml -- why3 config detect \
    && rm -rf /root/.opam/download-cache /root/.opam/log

FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Runtime-only OS packages.  cvc5 and CoL2Inv's verification tools need no
# compiler or development headers after the OPAM switch has been built.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        cpp \
        cvc5=1.1.2-1build1 \
        graphviz \
        libgmp10 \
        libgtksourceview-3.0-1 \
        python3 \
        python3-pip \
        python3-venv \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

# Frama-C invokes a command named "gcc" only for its preprocessing mode
# (`gcc -E`).  The standalone GNU preprocessor is sufficient and avoids
# shipping the compiler, linker and development runtime in the final image.
RUN ln -s /usr/bin/cpp /usr/local/bin/gcc

COPY --from=ocaml-builder /root/.opam/col2inv-ocaml /root/.opam/col2inv-ocaml

ENV PATH="/opt/col2inv-venv/bin:/root/.opam/col2inv-ocaml/bin:${PATH}" \
    OPAMSWITCH=col2inv-ocaml \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN why3 config detect

WORKDIR /opt/col2inv
COPY requirement.txt ./

RUN python3 -m venv /opt/col2inv-venv \
    && python -m pip install --no-cache-dir -r requirement.txt

COPY src/ ./src/
COPY Benchmark/ ./Benchmark/
COPY run_col2inv.sh README.md ./
COPY docker/ ./docker/

RUN chmod +x run_col2inv.sh docker/smoke_test.sh \
    && docker/smoke_test.sh

VOLUME ["/opt/col2inv/ResultQY45"]

ENTRYPOINT ["/opt/col2inv/run_col2inv.sh"]
CMD []
