# -*- coding: utf-8 -*-
"""
Painel KAT — servidor para HOSPEDAGEM (Render, Railway, etc.), com IA nativa.

Experiência de quem visualiza: abre o link e usa. NÃO digita senha nenhuma.

Como a chave fica protegida sem pedir senha:
  - A chave da Anthropic fica SOMENTE na variável de ambiente do host
    (ANTHROPIC_API_KEY) — nunca vai para o navegador nem para o repositório.
  - Acesso por TOKEN no link (opcional, recomendado): você compartilha
    https://SEU-APP/?t=SEU_TOKEN — a pessoa só clica; o servidor grava um cookie
    e ela nunca mais vê token nem senha. Sem o token, mostra "acesso restrito".
    Se você não definir ACCESS_TOKEN, o painel fica totalmente aberto.
  - Limite de requisições por IP (RATE_MAX/min) para evitar abuso.
  - (Fora daqui) coloque um LIMITE DE GASTO na chave, no console da Anthropic.

Variáveis de ambiente no host:
  ANTHROPIC_API_KEY  -> sua chave (nova, rotacionada)      [obrigatória p/ IA]
  ACCESS_TOKEN       -> um segredo p/ o link (ex.: kat-demo-9x2)  [opcional]
  MODELO             -> modelo da IA (padrão claude-haiku-4-5, mais barato;
                        pode trocar p/ claude-sonnet-5 ou claude-opus-4-8)  [opcional]
  RATE_MAX           -> requisições de IA por minuto por IP (padrão 30)
  PORT               -> definido automaticamente pelo host
"""
import base64, json, os, time, threading, collections
from urllib.parse import urlparse, parse_qs
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import pandas as pd

AQUI = os.path.dirname(os.path.abspath(__file__))
CANDIDATOS_BASE = [
    os.path.join(AQUI, "crm_kat_base_ficticia.xlsx"),
    os.path.join(os.path.dirname(AQUI), "dados", "crm_kat_base_ficticia.xlsx"),
]
BASE = next((p for p in CANDIDATOS_BASE if os.path.exists(p)), CANDIDATOS_BASE[0])
PORT = int(os.environ.get("PORT", os.environ.get("PORTA", "7799")))
HOST = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
RATE_MAX = int(os.environ.get("RATE_MAX", "30"))
# Modelo padrão: Haiku (mais barato). Trocável pela variável de ambiente MODELO
# (ex.: claude-sonnet-5 ou claude-opus-4-8) sem mexer no código.
MODELO = os.environ.get("MODELO", "claude-haiku-4-5")

try:
    import anthropic
except Exception:
    anthropic = None

# ---------------------------------------------------------------------------
# Contexto por visão (a partir da base fictícia)
# ---------------------------------------------------------------------------
import re
xl = pd.read_excel(BASE, sheet_name=None)
cli = xl["clientes"]; ass = xl["assessores"]; reun = xl["reunioes"]
inv = xl["investimentos"]; ser = xl["ecossistema_servicos"]
afi = xl.get("afi_planejamento", cli.iloc[0:0]); ativ = xl.get("atividades", cli.iloc[0:0])

sp = ass[(ass["unidade"] == "São Paulo") & (ass["cargo"].str.contains("Assessor"))]
cont = cli.groupby("assessor_id").size()
sp = sp.assign(n=sp["assessor_id"].map(cont).fillna(0)).sort_values("n")
ASSESSOR_ID = sp.iloc[len(sp)//2]["assessor_id"]
ASSESSOR_NOME = sp.iloc[len(sp)//2]["assessor"]
UNIDADE = "São Paulo"
ult_reuniao = reun.groupby("cliente_id")["data"].max()

def brl(v):
    v = float(v)
    if v >= 1e6: return f"R$ {v/1e6:.1f} mi"
    if v >= 1e3: return f"R$ {v/1e3:.0f} mil"
    return f"R$ {v:.0f}"

# --- Vencimentos (data-base da base fictícia = 15/07/2026) ---
HOJE = pd.Timestamp("2026-07-15")
inv["_venc"] = pd.to_datetime(inv["vencimento"], errors="coerce")
nome_por_id = dict(zip(cli["cliente_id"], cli["nome"]))

def proximos_vencimentos(ids, dias=60, cap=30):
    v = inv[inv["cliente_id"].isin(ids) & inv["_venc"].notna()]
    v = v[(v["_venc"] >= HOJE) & (v["_venc"] <= HOJE + pd.Timedelta(days=dias))].sort_values("_venc")
    if v.empty:
        return f"Nenhum vencimento nos próximos {dias} dias."
    linhas = []
    for _, x in v.head(cap).iterrows():
        nm = nome_por_id.get(x["cliente_id"], str(x["cliente_id"]))
        ur = ult_reuniao.get(x["cliente_id"])
        if pd.notna(ur):
            ur_dt = pd.to_datetime(ur)
            dias_contato = (HOJE - ur_dt).days
            contato = f"último contato {ur_dt.strftime('%d/%m/%Y')} (há {dias_contato} dias)"
        else:
            contato = "sem contato registrado"
        dias_venc = (x["_venc"] - HOJE).days
        linhas.append(f'- vence {x["_venc"].strftime("%d/%m/%Y")} (em {dias_venc}d) · {nm} · '
                      f'{x["ativo"]} ({x["indexador"]}) · {brl(x["valor_atual"])} · {contato}')
    extra = len(v) - cap
    if extra > 0:
        linhas.append(f"(+{extra} outros vencimentos no período)")
    return "\n".join(linhas)

def ecossistema_resumo(ids, top=None):
    s = ser[ser["cliente_id"].isin(ids) & (ser["status"] == "Ativo")]
    if s.empty:
        return "Nenhum serviço de ecossistema ativo no escopo."
    por_cat = s.groupby("categoria").agg(n=("cliente_id", "count"), rec=("receita_gerada_12m", "sum")).sort_values("rec", ascending=False)
    por_inst = s.groupby("instituicao").size().sort_values(ascending=False)
    L = [f"Total de serviços ativos: {len(s)}.", "Por categoria (ativos · receita 12m):"]
    for cat, r in por_cat.iterrows():
        L.append(f"- {cat}: {int(r.n)} · {brl(r.rec)}")
    seg = s[s["categoria"].str.startswith("Seguro")]
    if len(seg):
        L.append("Seguros por seguradora (ativos): " + ", ".join(f"{i} {int(n)}" for i, n in seg.groupby("instituicao").size().sort_values(ascending=False).items()))
    L.append("Por instituição (todos os contratos ativos):")
    items = list(por_inst.items())
    if top:
        items = items[:top]
    for inst, n in items:
        L.append(f"- {inst}: {int(n)}")
    if top and len(por_inst) > top:
        L.append(f"(+{len(por_inst)-top} outras instituições)")
    return "\n".join(L)

# mapas de assessor -> nome/unidade (para troca de usuário na visão Assessor)
nome_assessor = dict(zip(ass["assessor_id"], ass["assessor"]))
uni_assessor = dict(zip(ass["assessor_id"], ass["unidade"]))
UNIDADES = list(dict.fromkeys(cli["unidade"]))

def contexto_carteira(ids, limite=40):
    c = cli[cli["cliente_id"].isin(ids)].sort_values("patrimonio_investimentos_safra", ascending=False)
    out = []
    for _, r in c.head(limite).iterrows():
        ur = ult_reuniao.get(r["cliente_id"])
        ur = pd.to_datetime(ur).strftime("%d/%m/%Y") if pd.notna(ur) else "sem registro"
        out.append(f'- {r["nome"]} ({r["tipo"]}, {r["perfil_investidor"]}) | AUC {brl(r["patrimonio_investimentos_safra"])} | '
                   f'ecossistema {r["pct_adesao_ecossistema"]}% ({"apto" if r["ecossistema_apto"]=="Sim" else "não apto"}) | '
                   f'{r["num_classes_investimentos"]}/6 classes | AFI {r["tem_afi"]} | última reunião {ur} | '
                   f'receita 12m {brl(r["receita_total_12m"])}')
    return "\n".join(out)

def resumo(ids, titulo):
    c = cli[cli["cliente_id"].isin(ids)]
    return (f"{titulo}\nClientes: {len(c)} | PL/AUC total: {brl(c['patrimonio_investimentos_safra'].sum())} | "
        f"ecossistema médio: {c['pct_adesao_ecossistema'].mean():.0f}% | aptos: {(c['ecossistema_apto']=='Sim').sum()}\n"
        f"Receita 12m por segmento — Investimentos {brl(c['receita_investimentos_12m'].sum())}, "
        f"Seguros {brl(c['receita_seguros_12m'].sum())}, Consórcio {brl(c['receita_consorcio_12m'].sum())}, "
        f"Câmbio {brl(c['receita_cambio_12m'].sum())}, Crédito {brl(c['receita_credito_12m'].sum())}\n")

ids_ass = set(cli[cli["assessor_id"] == ASSESSOR_ID]["cliente_id"])
ids_uni = set(cli[cli["unidade"] == UNIDADE]["cliente_id"])
ids_all = set(cli["cliente_id"])

def por_assessor(ids):
    g = cli[cli["cliente_id"].isin(ids)].groupby("assessor").agg(
        clientes=("cliente_id","count"), AUC=("patrimonio_investimentos_safra","sum"),
        receita=("receita_total_12m","sum"), aptos=("ecossistema_apto", lambda s:(s=="Sim").sum()))
    return "\n".join(f'- {a}: {int(r.clientes)} clientes, AUC {brl(r.AUC)}, receita 12m {brl(r.receita)}, aptos {int(r.aptos)}'
                     for a, r in g.sort_values("receita", ascending=False).iterrows())

def por_unidade():
    g = cli.groupby("unidade").agg(clientes=("cliente_id","count"), AUC=("patrimonio_investimentos_safra","sum"),
        receita=("receita_total_12m","sum"), eco=("pct_adesao_ecossistema","mean"),
        aptos=("ecossistema_apto", lambda s:(s=="Sim").sum()))
    return "\n".join(f'- {u}: {int(r.clientes)} clientes, AUC {brl(r.AUC)}, receita 12m {brl(r.receita)}, '
                     f'ecossistema {r.eco:.0f}%, aptos {int(r.aptos)}'
                     for u, r in g.sort_values("receita", ascending=False).iterrows())

def assessores_por_unidade():
    g = cli.groupby(["unidade", "assessor"]).agg(
        clientes=("cliente_id", "count"), AUC=("patrimonio_investimentos_safra", "sum"),
        receita=("receita_total_12m", "sum"))
    out = []
    for uni in UNIDADES:
        if uni not in g.index.get_level_values(0):
            continue
        out.append(f"{uni}:")
        for (_, a), r in g.loc[[uni]].sort_values("receita", ascending=False).iterrows():
            out.append(f"  - {a}: {int(r.clientes)} clientes, AUC {brl(r.AUC)}, receita 12m {brl(r.receita)}")
    return "\n".join(out)

def receita_por_unidade():
    g = cli.groupby("unidade").agg(
        inv=("receita_investimentos_12m", "sum"), seg=("receita_seguros_12m", "sum"),
        con=("receita_consorcio_12m", "sum"), cam=("receita_cambio_12m", "sum"),
        cred=("receita_credito_12m", "sum")).sort_values("seg", ascending=False)
    return "\n".join(f"- {u}: Seguros {brl(r.seg)}, Consórcio {brl(r.con)}, Câmbio {brl(r.cam)}, "
                     f"Crédito {brl(r.cred)}, Investimentos {brl(r.inv)}" for u, r in g.iterrows())

def ids_de(scope, ent=None):
    if scope == "assessor" and ent in nome_assessor:
        return set(cli[cli["assessor_id"] == ent]["cliente_id"])
    if scope == "lider" and ent in set(cli["unidade"]):
        return set(cli[cli["unidade"] == ent]["cliente_id"])
    return {"assessor": ids_ass, "lider": ids_uni, "gestao": ids_all}.get(scope, ids_all)

_ctx_cache = {}
def contexto_de(scope, ent=None):
    key = (scope, ent)
    if key in _ctx_cache:
        return _ctx_cache[key]
    if scope == "assessor":
        ids = ids_de("assessor", ent)
        nome = nome_assessor.get(ent, ASSESSOR_NOME)
        uni = uni_assessor.get(ent, UNIDADE)
        ctx = (resumo(ids, f"VISÃO ASSESSOR — {nome} (Unidade {uni})") + "\nCLIENTES DA CARTEIRA:\n" + contexto_carteira(ids, 40)
            + "\n\nPRÓXIMOS VENCIMENTOS (60 dias, data-base 15/07/2026):\n" + proximos_vencimentos(ids, 60)
            + "\n\nECOSSISTEMA — SERVIÇOS ATIVOS:\n" + ecossistema_resumo(ids))
    elif scope == "lider":
        uni = ent if ent in set(cli["unidade"]) else UNIDADE
        ids = set(cli[cli["unidade"] == uni]["cliente_id"])
        ctx = (resumo(ids, f"VISÃO TEAM LEADER — Unidade {uni}") + "\nPOR ASSESSOR:\n" + por_assessor(ids) + "\n\nMAIORES CLIENTES:\n" + contexto_carteira(ids, 20)
            + "\n\nPRÓXIMOS VENCIMENTOS (60 dias, data-base 15/07/2026):\n" + proximos_vencimentos(ids, 60)
            + "\n\nECOSSISTEMA — SERVIÇOS ATIVOS:\n" + ecossistema_resumo(ids, top=20))
    else:
        ctx = (resumo(ids_all, "VISÃO GESTÃO — KAT (5 unidades, 20 assessores)") + "\nPOR UNIDADE:\n" + por_unidade()
            + "\n\nRECEITA POR UNIDADE E SEGMENTO (12m):\n" + receita_por_unidade()
            + "\n\nASSESSORES POR UNIDADE:\n" + assessores_por_unidade()
            + "\n\nPRÓXIMOS VENCIMENTOS (45 dias, data-base 15/07/2026):\n" + proximos_vencimentos(ids_all, 45)
            + "\n\nECOSSISTEMA — SERVIÇOS ATIVOS:\n" + ecossistema_resumo(ids_all, top=20))
    _ctx_cache[key] = ctx
    return ctx

_nomes_lower = [(str(r["nome"]).lower(), r["cliente_id"]) for _, r in cli.iterrows()]

def detalhe_cliente(cid):
    r = cli[cli["cliente_id"] == cid]
    if r.empty:
        return ""
    r = r.iloc[0]
    L = [f"\n=== DETALHE DO CLIENTE {r['nome']} ({cid}) ==="]
    L.append(f"Tipo {r['tipo']} · perfil {r['perfil_investidor']} · assessor {r['assessor']} · unidade {r['unidade']} · "
             f"cliente desde {pd.to_datetime(r['cliente_desde']).strftime('%d/%m/%Y')}")
    L.append(f"AUC {brl(r['patrimonio_investimentos_safra'])} · ecossistema {r['pct_adesao_ecossistema']}% "
             f"({'apto' if r['ecossistema_apto']=='Sim' else 'não apto'}) · AFI {r['tem_afi']} · receita 12m {brl(r['receita_total_12m'])}")
    h = inv[inv["cliente_id"] == cid]
    if len(h):
        L.append("Carteira: " + "; ".join(f"{x['produto']} {x['ativo']} ({x['classe']}, {brl(x['valor_atual'])})" for _, x in h.head(12).iterrows()))
    s = ser[(ser["cliente_id"] == cid) & (ser["status"] == "Ativo")]
    if len(s):
        L.append("Ecossistema ativo: " + "; ".join(f"{x['categoria']} — {x['instituicao']}" for _, x in s.iterrows()))
    a = afi[afi["cliente_id"] == cid]
    if len(a):
        a = a.iloc[0]
        L.append(f"AFI: renda {brl(a['renda_mensal'])}, {a['composicao_familiar']}; objetivos: {a['objetivos']}; projetos: {a['projetos']}")
    rr = reun[reun["cliente_id"] == cid].sort_values("data", ascending=False)
    if len(rr):
        L.append("Reuniões (mais recentes primeiro):")
        for _, x in rr.head(5).iterrows():
            L.append(f"- {pd.to_datetime(x['data']).strftime('%d/%m/%Y')} [{x['tipo']}]: {x['resumo_transcricao']} "
                     f"(insight AFI: {x['insight_afi']}; próxima ação: {x['proxima_acao']})")
    else:
        L.append("Reuniões: nenhuma registrada.")
    if len(ativ):
        at = ativ[ativ["cliente_id"] == cid]
        if len(at):
            L.append("Atividades da mesa: " + "; ".join(f"{x['produto']} ({x['status']})" for _, x in at.head(6).iterrows()))
    return "\n".join(L)

def buscar_cliente(pergunta, ids):
    """Se a pergunta cita um CPF/CNPJ ou o nome de um cliente, devolve o detalhe dele."""
    cid = None
    m = re.search(r"\d{2,3}[.\s]?\d{3}[.\s]?\d{3}[-/]?\d{2,4}[-]?\d{0,2}", pergunta)
    if m:
        digs = re.sub(r"\D", "", m.group(0))
        for c in cli["cliente_id"]:
            if re.sub(r"\D", "", str(c)) == digs:
                cid = c; break
    if cid is None:
        q = pergunta.lower()
        for nome_l, c in _nomes_lower:
            if len(nome_l) >= 8 and nome_l in q:
                cid = c; break
    if cid is None:
        return ""
    if cid not in ids:
        nome = cli[cli["cliente_id"] == cid].iloc[0]["nome"]
        return f"\n(O cliente {nome} existe, mas não pertence ao escopo/assessor desta visão — troque de visão ou de usuário para consultá-lo.)"
    return detalhe_cliente(cid)

SYSTEM = ("Você é o assistente de IA do CRM da KAT Investimentos (escritório de assessoria de investimentos). "
    "Responda em português (BR), objetivo e útil. Use SOMENTE os dados fornecidos (base fictícia de teste); "
    "se algo não estiver nos dados, diga que não há na base. Pode fazer contas, rankings e recomendações táticas. "
    "Se houver um bloco 'DETALHE DO CLIENTE', use-o para responder perguntas sobre esse cliente específico "
    "(reuniões e seus resumos, carteira, serviços do ecossistema, AFI). "
    "Para perguntas sobre vencimentos/maturidade, use o bloco 'PRÓXIMOS VENCIMENTOS' (cada linha traz a data do vencimento, "
    "o cliente, o ativo, o valor e a data do último contato). 'Ainda não contatado' = sem contato registrado ou último contato há muitos dias. "
    "A data de referência (hoje) é 15/07/2026. Seja conciso.\n\n=== DADOS DA VISÃO ATUAL ===\n")

def responder(scope, question, history, ent=None):
    if anthropic is None:
        return {"error": "Biblioteca 'anthropic' não instalada no servidor."}
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return {"error": "Servidor sem ANTHROPIC_API_KEY configurada."}
    if not str(question).strip():
        return {"error": "Pergunta vazia."}
    ctx = contexto_de(scope, ent)
    try:
        ctx += buscar_cliente(str(question), ids_de(scope, ent))
    except Exception:
        pass
    msgs = [{"role": h["role"], "content": str(h["content"])[:4000]}
            for h in (history or [])[-8:] if h.get("role") in ("user","assistant") and h.get("content")]
    msgs.append({"role": "user", "content": str(question)[:4000]})
    try:
        resp = anthropic.Anthropic().messages.create(model=MODELO, max_tokens=1200, system=SYSTEM + ctx, messages=msgs)
        txt = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        return {"answer": txt.strip() or "(sem resposta)"}
    except Exception as e:
        return {"error": f"Erro ao chamar a IA: {e}"}

# --- limite de requisições por IP (proteção de custo, invisível p/ o usuário) ---
_hits = collections.defaultdict(list)
_lock = threading.Lock()
def rate_ok(ip):
    now = time.time()
    with _lock:
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.pop(0)
        if len(q) >= RATE_MAX:
            return False
        q.append(now)
        return True

PAGINA_NEGADO = ("<!doctype html><meta charset=utf-8><title>Acesso restrito</title>"
    "<div style='font-family:system-ui;max-width:460px;margin:16vh auto;text-align:center;color:#1f4a43'>"
    "<div style='font-size:26px;font-weight:800;letter-spacing:3px'>KAT <span style='color:#c4a461'>INVESTIMENTOS</span></div>"
    "<p style='margin-top:18px;color:#5b6b67'>Acesso restrito. Use o link de convite que você recebeu "
    "(ele contém o código de acesso).</p></div>")

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=AQUI, **k)

    def _ip(self):
        return (self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or self.client_address[0])

    def _acesso(self):
        """'ok' = liberado; 'set' = token válido no link (gravar cookie); 'no' = negado."""
        if not ACCESS_TOKEN:
            return "ok"
        if f"kat_sess={ACCESS_TOKEN}" in self.headers.get("Cookie", ""):
            return "ok"
        if parse_qs(urlparse(self.path).query).get("t", [""])[0] == ACCESS_TOKEN:
            return "set"
        return "no"

    def _negar_html(self):
        body = PAGINA_NEGADO.encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        acc = self._acesso()
        if acc == "no":
            return self._negar_html()
        if acc == "set":
            # grava cookie e redireciona para a URL limpa (some o token da barra)
            self.send_response(302)
            self.send_header("Set-Cookie", f"kat_sess={ACCESS_TOKEN}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
            self.send_header("Location", "/")
            self.end_headers()
            return
        if self.path in ("/", ""):
            self.path = "/painel_kat.html"
        return super().do_GET()

    def do_POST(self):
        if self._acesso() == "no":
            return self._json({"error": "Acesso não autorizado."}, 403)
        if self.path.split("?")[0] == "/api/chat":
            if not rate_ok(self._ip()):
                return self._json({"error": "Muitas perguntas em pouco tempo. Aguarde alguns segundos."}, 429)
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                data = {}
            return self._json(responder(data.get("scope", "assessor"), data.get("question", ""), data.get("history"), data.get("ent")))
        self.send_error(404, "Not found")

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    tem_chave = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    print(f"Painel KAT em http://{HOST}:{PORT} | acesso: {'token no link' if ACCESS_TOKEN else 'ABERTO'} "
          f"| limite IA: {RATE_MAX}/min por IP | IA: {'ativa' if tem_chave else 'sem chave'}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
