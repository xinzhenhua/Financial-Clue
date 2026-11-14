#!/usr/bin/env python3
"""财产线索抓取（免费源，失败不崩）。
每个来源独立运行，报告 ok/error/count；任何一个挂了都不影响其他来源与手动链接。
环境变量：TARGET_COMPANY（公司全称，空则读 companies.txt）、TYC_TOKEN（可选，天眼查开放平台）
"""
import os, re, json, sys, time, datetime, html
from urllib.parse import quote
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
TIMEOUT = 20


def safe_filename(name):
    return re.sub(r'[\\/:*?"<>|\s]', '_', name).strip()


def core_name(name):
    return re.sub(r'(股份)?有限(责任)?公司$', '', name).strip() or name


def strip_tags(s):
    return html.unescape(re.sub(r'<[^>]+>', '', s or '')).strip()


# ---------- 来源 ----------
def src_ggzy(name):
    """全国公共资源交易平台：交易公告检索（接口为记忆所得，未验证；失败会如实报告）"""
    url = "https://deal.ggzy.gov.cn/ds/deal/dealList_find.jsp"
    data = {"TIMEBEGIN_SHOW": "", "TIMEEND_SHOW": "", "TIMEBEGIN": "", "TIMEEND": "",
            "SOURCE_TYPE": "1", "DEAL_TIME": "06", "DEAL_CLASSIFY": "01", "DEAL_STAGE": "0100",
            "DEAL_PROVINCE": "0", "DEAL_CITY": "0", "DEAL_PLATFORM": "0", "BID_PLATFORM": "0",
            "DEAL_TRADE": "0", "isShowAll": "1", "PAGENUMBER": "1", "FINDTXT": name}
    r = S.post(url, data=data, timeout=TIMEOUT, headers={"Referer": "https://deal.ggzy.gov.cn/ds/deal/dealList.jsp"})
    r.raise_for_status()
    items = []
    try:
        j = r.json()
        rows = j.get("data") or []
        for x in rows:
            items.append({"title": strip_tags(x.get("title", "")),
                          "snippet": " ".join(strip_tags(str(x.get(k, ""))) for k in ("classifyShow", "stageShow", "districtShow", "platformName", "timeShow")),
                          "url": x.get("url", ""), "source": "全国公共资源交易平台"})
    except ValueError:
        raise RuntimeError("返回非JSON（可能被反爬或接口已变）")
    return items


def src_ccgp(name):
    """中国政府采购网：全文检索（HTML 解析）"""
    today = datetime.date.today()
    start = today - datetime.timedelta(days=365 * 3)
    fmt = lambda d: d.strftime("%Y:%m:%d")
    url = ("https://search.ccgp.gov.cn/bxsearch?searchtype=1&page_index=1&bidSort=0&buyerName=&projectId="
           "&pinMu=0&bidType=0&dbselect=bidx&kw=%s&start_time=%s&end_time=%s&timeType=6"
           "&displayZone=&zoneId=&pppStatus=0&agentName=") % (quote(name), fmt(start), fmt(today))
    r = S.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    items = []
    for m in re.finditer(r'<li>\s*<a href="(http[^"]+)"[^>]*>(.*?)</a>(.*?)</li>', r.text, re.S):
        title = strip_tags(m.group(2))
        rest = strip_tags(re.sub(r'<span>', ' ', m.group(3)))
        if title:
            items.append({"title": title, "snippet": re.sub(r'\s+', ' ', rest)[:300],
                          "url": m.group(1), "source": "中国政府采购网"})
    return items


QUERIES = ['"{n}" 中标', '"{n}" 工程款 判决', '"{n}" 项目部 施工', '"{n}" 竣工验收', '"{n}" 被执行人 执行']


def src_search(name):
    """必应搜索（HTML，低频使用）。"""
    items, errs = [], 0
    for q in QUERIES:
        try:
            r = S.get("https://cn.bing.com/search", params={"q": q.format(n=name), "setlang": "zh-cn"}, timeout=TIMEOUT)
            r.raise_for_status()
            for m in re.finditer(r'<li class="b_algo".*?</li>', r.text, re.S):
                blk = m.group(0)
                a = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
                if not a:
                    continue
                p = re.search(r'<p[^>]*>(.*?)</p>', blk, re.S)
                items.append({"title": strip_tags(a.group(2)), "snippet": strip_tags(p.group(1)) if p else "",
                              "url": html.unescape(a.group(1)), "source": "必应:" + q.format(n="").strip()})
        except Exception:
            errs += 1
        time.sleep(1.5)
    if errs == len(QUERIES):
        raise RuntimeError("全部查询失败（可能被拦截）")
    return items


def src_tyc(name):
    """可选：天眼查开放平台（需 TYC_TOKEN，付费）。无 token 直接跳过。"""
    tok = os.environ.get("TYC_TOKEN", "").strip()
    if not tok:
        raise SkipProvider("未配置 TYC_TOKEN（可选，付费）")
    r = S.get("https://open.api.tianyancha.com/services/open/search/2.0",
              params={"word": name}, headers={"Authorization": tok}, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    res = (j.get("result") or {}).get("items") or []
    return [{"title": x.get("name", ""), "snippet": " ".join(str(x.get(k, "")) for k in ("legalPersonName", "regCapital", "regStatus", "base")),
             "url": "", "source": "天眼查API"} for x in res]


class SkipProvider(Exception):
    pass


PROVIDERS = [("ggzy", src_ggzy), ("ccgp", src_ccgp), ("search", src_search), ("tyc", src_tyc)]


# ---------- 线索提取 ----------
CASE_RE = re.compile(r'[（(]\d{4}[）)][一-龥A-Za-z0-9]{1,12}?\d*[一-龥]{0,4}\d+号')
AMT_RE = re.compile(r'(\d+(?:[.,]\d+)?)\s*(亿元|亿|万元|万|元)')
PROJ_RE = re.compile(r'[一-龥A-Za-z0-9（）()·\-]{3,40}?(?:工程项目|项目|工程|标段)(?!款|价款|有限|公司|施工|合同|纠纷|总承包|质量|保证)')
CO_RE = re.compile(r'[一-龥（）()]{3,30}?(?:股份有限公司|有限责任公司|有限公司|集团|公司)')


def to_wan(num, unit):
    v = float(num.replace(',', ''))
    return v * 10000 if unit.startswith('亿') else v if unit.startswith('万') else v / 10000


def extract(items, name):
    core = core_name(name)
    leads, seen = [], set()
    for it in items:
        text = f"{it['title']} {it['snippet']}"
        if core not in text:
            continue
        key = (it['title'], it['url'])
        if key in seen:
            continue
        seen.add(key)
        amts = [to_wan(a, u) for a, u in AMT_RE.findall(text)]
        wan = round(max(amts), 2) if amts else ""
        cases = CASE_RE.findall(text)
        projs = [p for p in PROJ_RE.findall(text) if core not in p][:1]
        owners = [re.sub(r'^[与和及跟、，,的]+', '', c) for c in CO_RE.findall(text) if core not in c and c != name][:1]
        if re.search(r'工程款|欠付|结算|判决|裁定|纠纷', text):
            typ = '应收工程款'
        elif re.search(r'中标|成交|中选|施工|竣工|项目', text):
            typ = '项目(在建/完工)'
        else:
            typ = '其他'
        leads.append({"项目名称": projs[0] if projs else it['title'][:40], "业主": owners[0] if owners else "",
                      "金额(万元)": wan, "案号": cases[0] if cases else "", "来源": it['url'] or it['source'],
                      "类型": typ, "备注": f"{it['source']}｜{it['title'][:60]}"})
    return leads


def manual_links(name):
    q = quote(name)
    return [
        {"name": "爱企查", "url": f"https://aiqicha.baidu.com/s?q={q}", "tip": "股权/对外投资/司法风险/招投标（免登录部分可看）"},
        {"name": "企查查", "url": f"https://www.qcc.com/web/search?key={q}", "tip": "被执行人/限高/裁判文书/招投标"},
        {"name": "天眼查", "url": f"https://www.tianyancha.com/search?key={q}", "tip": "同上，交叉验证"},
        {"name": "四库一平台", "url": f"https://jzsc.mohurd.gov.cn/data/company?complexname={q}", "tip": "在建/已完工项目、业绩（需验证码）"},
        {"name": "中国执行信息公开网", "url": "https://zxgk.court.gov.cn/zhzxgk/", "tip": "被执行人/终本/限高（需验证码）"},
        {"name": "裁判文书网", "url": "https://wenshu.court.gov.cn/", "tip": "搜被告为原告的工程款案（需登录）"},
        {"name": "国家企业信用信息公示系统", "url": "https://www.gsxt.gov.cn/", "tip": "股权出质/动产抵押/年报（需验证码）"},
        {"name": "中登网", "url": "https://www.zhongdengwang.org.cn/", "tip": "应收账款质押/融资租赁登记"},
        {"name": "全国公共资源交易平台", "url": "https://deal.ggzy.gov.cn/ds/deal/dealList.jsp", "tip": "手动搜公司名看中标项目"},
        {"name": "百度", "url": f"https://www.baidu.com/s?wd={quote(chr(34)+name+chr(34)+' 中标')}", "tip": "补漏"},
    ]


def run_one(name):
    out = {"company": name, "updated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "providers": {}, "items": [], "leads": [], "links": manual_links(name)}
    for pid, fn in PROVIDERS:
        try:
            its = fn(name)
            out["providers"][pid] = {"ok": True, "count": len(its)}
            out["items"] += its
        except SkipProvider as e:
            out["providers"][pid] = {"ok": False, "skipped": True, "error": str(e), "count": 0}
        except Exception as e:
            out["providers"][pid] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200], "count": 0}
        print(f"[{pid}]", out["providers"][pid], file=sys.stderr)
    out["leads"] = extract(out["items"], name)
    return out


def main():
    target = (os.environ.get("TARGET_COMPANY") or "").strip()
    names = [target] if target else []
    if not names and os.path.exists("companies.txt"):
        names = [l.strip() for l in open("companies.txt", encoding="utf-8") if l.strip() and not l.startswith("#")]
    if not names:
        print("没有目标公司", file=sys.stderr)
        return
    os.makedirs("data", exist_ok=True)
    for n in names:
        res = run_one(n)
        with open(os.path.join("data", safe_filename(n) + ".json"), "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    # 永远 exit 0：单源失败不应让工作流变红


if __name__ == "__main__":
    main()
