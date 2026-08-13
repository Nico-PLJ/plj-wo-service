# =====================================================================
# RECEBIDO NO PERÍODO — cole este bloco no fim do main.py
# (antes da seção da PLANILHA, ou no fim do arquivo; tanto faz)
#
# Por que isto é necessário: o /qb/abertas só enxerga fatura EM ABERTO.
# Quando o cliente paga, o projeto some daquela lista — então não dá para
# saber por ali quanto entrou no mês. Os Payment do QuickBooks são o
# registro da baixa, e é isso que esta rota lê.
# =====================================================================

_rec_cache = {}


def qb_recebido_periodo(ini, fim):
    """Soma, por projeto, o dinheiro que entrou entre duas datas."""
    chave = str(ini) + ".." + str(fim)
    guardado = _rec_cache.get(chave)
    if guardado and (time.time() - guardado["quando"]) < 600:
        return guardado["dados"]

    todos, pos = [], 1
    for _ in range(6):                      # até 6 mil baixas no período
        q = ("select * from Payment "
             "where TxnDate >= '" + _esc_sql(ini) + "' "
             "and TxnDate <= '" + _esc_sql(fim) + "' "
             "startposition " + str(pos) + " maxresults 1000")
        lote = qb_query(q).get("Payment") or []
        todos += lote
        if len(lote) < 1000:
            break
        pos += 1000

    por = {}
    for p in todos:
        ref = p.get("CustomerRef") or {}
        nome = ref.get("name") or ""
        if not nome:
            continue
        # UnappliedAmt é adiantamento ainda não amarrado a uma fatura:
        # entrou no caixa, mas não abateu nada. Fica de fora.
        valor = float(p.get("TotalAmt") or 0) - float(p.get("UnappliedAmt") or 0)
        if valor <= 0:
            continue
        d = por.setdefault(nome, {"projeto": nome,
                                  "id": ref.get("value") or "",
                                  "recebido": 0.0, "qtd": 0})
        d["recebido"] += valor
        d["qtd"] += 1

    lista = sorted(por.values(), key=lambda x: -x["recebido"])
    for d in lista:
        d["recebido"] = round(d["recebido"], 2)
    _rec_cache[chave] = {"quando": time.time(), "dados": lista}
    return lista


@app.get("/qb/recebido")
def qb_recebido(ini: str = "", fim: str = "",
                authorization: str = Header(default="")):
    """Quanto cada projeto pagou no período. Usado nas metas por PM.

    Sem datas, assume do dia 1º do mês corrente até hoje.
    """
    quem_e(authorization)                   # basta estar cadastrado no schedule
    hoje = _date.today()
    if not ini:
        ini = hoje.replace(day=1).isoformat()
    if not fim:
        fim = hoje.isoformat()
    lista = qb_recebido_periodo(ini, fim)
    return {"ok": True, "ini": ini, "fim": fim,
            "total": round(sum(d["recebido"] for d in lista), 2),
            "projetos": lista}
