# -*- coding: utf-8 -*-
"""
agente/servidor.py
Conversa HTTP com o sistema (Flask) — só biblioteca padrão, para a máquina do
operador precisar de uma dependência a menos.

O token vai no cabeçalho `X-Token`. É ele que diz ao servidor QUEM é o
operador: o nome nunca vai no corpo (ver CLAUDE.md, seção 10-B).
"""
import json
import urllib.error
import urllib.parse
import urllib.request


class ErroServidor(Exception):
    """O servidor não respondeu ou respondeu algo que não é JSON."""


class TokenInvalido(ErroServidor):
    """401: token ausente, errado ou revogado no painel."""


class Cliente:
    def __init__(self, url_base, token, timeout=20):
        self.url_base = url_base.rstrip("/")
        self.token = (token or "").strip()
        self.timeout = timeout

    def _pedir(self, metodo, caminho, corpo=None, query=None):
        url = self.url_base + caminho
        if query:
            url += "?" + urllib.parse.urlencode(query)
        dados = None
        cab = {"X-Token": self.token, "Accept": "application/json"}
        if corpo is not None:
            dados = json.dumps(corpo).encode("utf-8")
            cab["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=dados, headers=cab, method=metodo)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise TokenInvalido("o servidor recusou o token: peça um novo ao "
                                    "coordenador no painel da reclassificação")
            try:
                corpo_erro = json.loads(e.read().decode("utf-8"))
                msg = corpo_erro.get("erro") or corpo_erro.get("msg") or str(corpo_erro)
            except Exception:
                msg = str(e)
            raise ErroServidor(f"{metodo} {caminho}: HTTP {e.code} — {msg}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise ErroServidor(f"sem contato com {self.url_base}: {e}")
        except json.JSONDecodeError:
            raise ErroServidor(f"{metodo} {caminho}: resposta não é JSON")

    def get(self, caminho, **query):
        return self._pedir("GET", caminho, query=query or None)

    def post(self, caminho, corpo=None):
        return self._pedir("POST", caminho, corpo=corpo or {})
