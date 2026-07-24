"""
scripts/fiscal_ingestao.py
Camada de INGESTÃO do módulo fiscal — a primeira do pipeline:

    [Ingestão] -> Parser -> De-para -> Motor de custo -> Motor de preço -> Fila

Regra de ouro: toda origem de XML fica atrás da interface `FornecedorDeXml`,
com um único método `obter_novos()` que devolve XMLs brutos. Trocar a origem
(upload manual, API de terceiro, futura integração SEFAZ) NÃO exige mudança em
nenhuma outra camada — o parser e o resto só recebem `XmlBruto`.

Implementações entregues:
  • UploadManual — arquivo único ou lote .zip
  • ApiTerceiro  — cliente REST configurável (URL, token, CNPJ)

Ponto de extensão futuro (NÃO implementado aqui):
  • SefazDistribuicaoDFe — consumiria o webservice NFeDistribuicaoDFe da SEFAZ
    (distribuição de DF-e por CNPJ + NSU). Para adicioná-lo, basta criar uma
    classe que implemente `FornecedorDeXml.obter_novos()` devolvendo `XmlBruto`
    com `origem="sefaz_dfe"`. Nenhuma outra camada muda. Ver o esqueleto
    documentado no fim do arquivo.

Esta camada é deliberadamente burra: ela NÃO parseia, NÃO valida NF-e, NÃO
deduplica por chave (isso é do parser + modelo `upsert_nota_fiscal`, que já é
idempotente). Só entrega bytes de XML rotulados pela origem.
"""
import io
import json
import zipfile
import urllib.request
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, List, Tuple


@dataclass
class XmlBruto:
    """Um XML de NF-e como veio da origem, ainda não parseado."""
    conteudo: bytes
    origem: str = "upload_manual"     # upload_manual | api_terceiro | sefaz_dfe
    nome: str = ""                    # nome de arquivo, quando houver (diagnóstico)

    def texto(self, encoding="utf-8") -> str:
        return self.conteudo.decode(encoding, errors="replace")


class FornecedorDeXml(ABC):
    """Contrato único da ingestão. Qualquer origem de XML implementa isto.

    Implemente SÓ `obter_novos()`. O consumidor (o importador, na camada de
    de-para) faz: `for xml in fornecedor.obter_novos(): parse(xml)`. A troca de
    origem é transparente para ele.
    """

    @abstractmethod
    def obter_novos(self) -> Iterable[XmlBruto]:
        """Devolve os XMLs disponíveis nesta origem. Pode ser um gerador."""
        raise NotImplementedError


# ── Upload manual (arquivo único ou lote .zip) ────────────────────────────────
def _eh_zip(nome: str, conteudo: bytes) -> bool:
    return (nome or "").lower().endswith(".zip") or conteudo[:4] == b"PK\x03\x04"


def _eh_xml(nome: str) -> bool:
    return (nome or "").lower().endswith(".xml")


class UploadManual(FornecedorDeXml):
    """Recebe arquivos enviados pelo usuário. Aceita:
      • um ou vários XMLs soltos, e/ou
      • um ou vários .zip contendo XMLs (extraídos recursivamente, 1 nível).
    Entrada: lista de (nome, bytes). Arquivos que não são .xml nem .zip e
    membros não-.xml dentro do zip são ignorados silenciosamente.
    """

    def __init__(self, arquivos: List[Tuple[str, bytes]]):
        # aceita também um único (nome, bytes) por conveniência
        if arquivos and isinstance(arquivos, tuple) and len(arquivos) == 2 \
                and isinstance(arquivos[0], str):
            arquivos = [arquivos]
        self._arquivos = list(arquivos or [])

    def obter_novos(self) -> Iterable[XmlBruto]:
        for nome, conteudo in self._arquivos:
            conteudo = conteudo if isinstance(conteudo, (bytes, bytearray)) \
                else str(conteudo).encode("utf-8")
            if _eh_zip(nome, conteudo):
                yield from self._extrair_zip(conteudo)
            elif _eh_xml(nome) or conteudo.lstrip()[:1] == b"<":
                yield XmlBruto(conteudo=bytes(conteudo), origem="upload_manual", nome=nome)

    @staticmethod
    def _extrair_zip(conteudo: bytes) -> Iterable[XmlBruto]:
        try:
            with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
                for membro in z.namelist():
                    if _eh_xml(membro):
                        yield XmlBruto(conteudo=z.read(membro), origem="upload_manual",
                                       nome=membro)
        except zipfile.BadZipFile:
            return


# ── API de terceiro (REST configurável) ───────────────────────────────────────
class ApiTerceiro(FornecedorDeXml):
    """Cliente REST genérico para um agregador de XMLs (ex.: Arquivei, NFe.io,
    SIEG e similares). Configurável por URL, token e CNPJ — sem hardcode de
    fornecedor. Usa só a stdlib (`urllib`), sem dependência nova.

    Contrato padrão assumido (o mais comum entre os agregadores) — ajuste o
    `mapear_resposta` se o seu provedor divergir, sem tocar em outra camada:
        GET {url}?cnpj={cnpj}[&extra...]
        Header: Authorization: Bearer {token}
        Resposta JSON: lista de objetos, cada um com uma chave de XML. Aceita
        as chaves usuais: "xml", "xmlNfe", "conteudo" (texto do XML) — ou uma
        lista de strings XML cruas.

    Falha de rede/HTTP é tolerada: devolve vazio e registra a exceção em
    `self.ultimo_erro`, para o importador seguir sem quebrar (degradação
    graciosa — o upload manual continua funcionando).
    """

    def __init__(self, url: str, token: str = "", cnpj: str = "",
                 parametros: dict = None, timeout: int = 30):
        self.url = url
        self.token = token
        self.cnpj = cnpj
        self.parametros = parametros or {}
        self.timeout = timeout
        self.ultimo_erro = None

    def obter_novos(self) -> Iterable[XmlBruto]:
        self.ultimo_erro = None
        try:
            dados = self._get()
        except Exception as e:  # rede/HTTP/JSON — não derruba a importação
            self.ultimo_erro = str(e)
            return
        for xml_txt in self.mapear_resposta(dados):
            if xml_txt:
                yield XmlBruto(conteudo=xml_txt.encode("utf-8"), origem="api_terceiro")

    def _get(self):
        params = {"cnpj": self.cnpj, **self.parametros} if self.cnpj else dict(self.parametros)
        url = self.url + (("?" + urllib.parse.urlencode(params)) if params else "")
        req = urllib.request.Request(url, method="GET")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            corpo = resp.read().decode("utf-8", errors="replace")
        corpo = corpo.strip()
        if corpo[:1] in ("[", "{"):
            return json.loads(corpo)
        return corpo  # XML cru (documento único)

    @staticmethod
    def mapear_resposta(dados) -> Iterable[str]:
        """Extrai os textos de XML da resposta. Ponto único de adaptação por
        provedor (isole aqui qualquer formato específico)."""
        if isinstance(dados, str):
            yield dados
            return
        if isinstance(dados, dict):
            dados = dados.get("notas") or dados.get("documentos") or dados.get("data") or [dados]
        for item in dados or []:
            if isinstance(item, str):
                yield item
            elif isinstance(item, dict):
                yield item.get("xml") or item.get("xmlNfe") or item.get("conteudo") or ""


# ── Ponto de extensão futuro (esqueleto documentado, NÃO implementar agora) ────
#
# class SefazDistribuicaoDFe(FornecedorDeXml):
#     """Consumiria o webservice NFeDistribuicaoDFe da SEFAZ, que entrega os
#     DF-e emitidos CONTRA um CNPJ, paginados por NSU (Número Sequencial Único).
#     Precisa de certificado digital A1/A3 (mTLS) e controle do último NSU
#     consumido (persistir para não reprocessar).
#
#     Implementação futura deve APENAS:
#       1. montar o SOAP distDFeInt (cUFAutor, CNPJ, distNSU/ultNSU);
#       2. chamar o endpoint com o certificado;
#       3. descompactar os docZip (gzip+base64) de cada docZip retornado;
#       4. devolver XmlBruto(conteudo=..., origem="sefaz_dfe") para cada NF-e.
#     Nenhuma outra camada muda: o importador continua chamando obter_novos().
#     """
#     def obter_novos(self):
#         raise NotImplementedError("Integração SEFAZ ainda não implementada.")
