"""
scripts/certificado.py
======================
HTTPS na rede da loja, com uma autoridade certificadora (CA) PRÓPRIA.

Por que existe:
  Por http://192.168.0.52 a senha viaja aberta no Wi-Fi, e o navegador só
  libera microfone (o Jardél, seção 19) e câmera em página segura. Não há
  domínio público para pedir um certificado de verdade, então o sistema cria a
  própria CA e assina com ela o certificado do servidor.

Como funciona:
  • `dados/tls/ca.crt` + `ca.key` — a CA, válida por 10 anos. É ela que cada
    aparelho instala UMA vez (página /certificado). Nunca é trocada sozinha:
    trocar a CA obrigaria a reinstalar em todos os aparelhos.
  • `dados/tls/servidor.crt` + `servidor.key` — o certificado do servidor,
    assinado pela CA, com os IPs e nomes desta máquina. Vale 800 dias (o
    iPhone recusa certificado de servidor com mais de 825) e é REFEITO sozinho
    quando faltam menos de 60 dias ou quando o IP da máquina muda — como a CA
    é a mesma, os aparelhos não percebem.
  • A chave da CA nunca sai da máquina; o backup copia a pasta (ver backup.py).

Camada de lógica: NÃO importa Flask.
"""
import datetime as _dt
import ipaddress
import os
import socket

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR_TLS = os.path.join(BASE_DIR, "dados", "tls")
CA_CRT = os.path.join(DIR_TLS, "ca.crt")
CA_KEY = os.path.join(DIR_TLS, "ca.key")
SRV_CRT = os.path.join(DIR_TLS, "servidor.crt")
SRV_KEY = os.path.join(DIR_TLS, "servidor.key")

NOME_CA = "Sistema de Ferramentas - CA da loja"
DIAS_CA = 3650
DIAS_SERVIDOR = 800
RENOVAR_COM_DIAS = 60


def disponivel():
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def ips_locais():
    """IPv4 desta máquina (sem loopback), o da rota padrão primeiro."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # não envia nada; só escolhe a rota
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def nomes_locais():
    nome = socket.gethostname()
    return [n for n in dict.fromkeys([nome, nome.lower(), "localhost"]) if n]


def _agora():
    return _dt.datetime.now(_dt.timezone.utc)


def _gravar(caminho, dados):
    os.makedirs(DIR_TLS, exist_ok=True)
    tmp = caminho + ".tmp"
    with open(tmp, "wb") as f:
        f.write(dados)
    os.replace(tmp, caminho)


def _chave_nova():
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.generate_private_key(ec.SECP256R1())


def _pem_chave(chave):
    from cryptography.hazmat.primitives import serialization
    return chave.private_bytes(serialization.Encoding.PEM,
                               serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())


def _pem_cert(cert):
    from cryptography.hazmat.primitives import serialization
    return cert.public_bytes(serialization.Encoding.PEM)


def _carregar_ca():
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    with open(CA_CRT, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    with open(CA_KEY, "rb") as f:
        chave = serialization.load_pem_private_key(f.read(), password=None)
    return cert, chave


def _criar_ca():
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes
    chave = _chave_nova()
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NOME_CA),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Sistema de Ferramentas")])
    agora = _agora()
    cert = (x509.CertificateBuilder()
            .subject_name(nome).issuer_name(nome)
            .public_key(chave.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(agora - _dt.timedelta(days=1))
            .not_valid_after(agora + _dt.timedelta(days=DIAS_CA))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True,
                                         crl_sign=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False,
                                         decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(chave.public_key()),
                           critical=False)
            .sign(chave, hashes.SHA256()))
    _gravar(CA_KEY, _pem_chave(chave))
    _gravar(CA_CRT, _pem_cert(cert))
    return cert, chave


def _criar_servidor(ca_cert, ca_chave, ips, nomes):
    from cryptography import x509
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    from cryptography.hazmat.primitives import hashes
    chave = _chave_nova()
    alt = [x509.DNSName(n) for n in nomes]
    alt += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in list(ips) + ["127.0.0.1"]]
    agora = _agora()
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                                        (ips or nomes or ["localhost"])[0])]))
            .issuer_name(ca_cert.subject)
            .public_key(chave.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(agora - _dt.timedelta(days=1))
            .not_valid_after(agora + _dt.timedelta(days=DIAS_SERVIDOR))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_chave.public_key()),
                           critical=False)
            .sign(ca_chave, hashes.SHA256()))
    _gravar(SRV_KEY, _pem_chave(chave))
    _gravar(SRV_CRT, _pem_cert(cert))
    return cert


def _motivo_refazer(ips, nomes):
    """None se o certificado do servidor serve; senão, o porquê."""
    from cryptography import x509
    if not (os.path.isfile(SRV_CRT) and os.path.isfile(SRV_KEY)):
        return "não existe"
    with open(SRV_CRT, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    if cert.not_valid_after_utc - _agora() < _dt.timedelta(days=RENOVAR_COM_DIAS):
        return "vence em breve"
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return "sem nomes"
    tem_ips = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
    tem_nomes = set(san.get_values_for_type(x509.DNSName))
    if not set(ips) <= tem_ips or not set(nomes) <= tem_nomes:
        return "IP ou nome da máquina mudou"
    return None


def garantir(ips=None, nomes=None):
    """Cria a CA (se faltar) e o certificado do servidor (se faltar, estiver
    perto de vencer ou não cobrir os IPs de hoje). Devolve
    {cert, chave, ca, ips, nomes, refeito: motivo|None}."""
    ips = list(ips if ips is not None else ips_locais())
    nomes = list(nomes if nomes is not None else nomes_locais())
    if os.path.isfile(CA_CRT) and os.path.isfile(CA_KEY):
        ca_cert, ca_chave = _carregar_ca()
    else:
        ca_cert, ca_chave = _criar_ca()
    motivo = _motivo_refazer(ips, nomes)
    if motivo:
        _criar_servidor(ca_cert, ca_chave, ips, nomes)
    return {"cert": SRV_CRT, "chave": SRV_KEY, "ca": CA_CRT,
            "ips": ips, "nomes": nomes, "refeito": motivo}


def info():
    """Dados para a página /certificado (sem chave nenhuma)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    if not os.path.isfile(CA_CRT):
        return {"existe": False}
    with open(CA_CRT, "rb") as f:
        ca = x509.load_pem_x509_certificate(f.read())
    sha = ca.fingerprint(hashes.SHA256()).hex().upper()
    out = {"existe": True,
           "ca_validade": ca.not_valid_after_utc.astimezone().strftime("%d/%m/%Y"),
           "ca_impressao": ":".join(sha[i:i + 2] for i in range(0, len(sha), 2)),
           "servidor_validade": None, "servidor_nomes": []}
    if os.path.isfile(SRV_CRT):
        with open(SRV_CRT, "rb") as f:
            srv = x509.load_pem_x509_certificate(f.read())
        out["servidor_validade"] = srv.not_valid_after_utc.astimezone().strftime("%d/%m/%Y")
        try:
            san = srv.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            out["servidor_nomes"] = ([str(i) for i in san.get_values_for_type(x509.IPAddress)]
                                     + list(san.get_values_for_type(x509.DNSName)))
        except x509.ExtensionNotFound:
            pass
    return out
