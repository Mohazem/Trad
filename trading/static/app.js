"use strict";
const $ = id => document.getElementById(id);
let busy = false, ready = false;
const labels = {closed: "Marché fermé · derniers cours disponibles", fresh: "Données récentes", stale: "Données anciennes / historique insuffisant", error: "Source indisponible"};
const actions = {buy: "Proposition d’achat", sell: "Proposition de vente", abstain: "Abstention"};
function node(tag, text, cls) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; }
function svgElement(tag, attrs) { const el = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const [k,v] of Object.entries(attrs)) el.setAttribute(k, v); return el; }
function chart(bars, symbol) {
  const svg = svgElement("svg", {viewBox:"0 0 420 200", role:"img", "aria-label":`Cours ${symbol}, dernières bougies minute`, class:"chart"});
  const title = svgElement("title", {}); title.textContent = `Clôtures observées pour ${symbol}`; svg.append(title);
  const vals = bars.map(b => Number(b.close));
  const min = Math.min(...vals), max = Math.max(...vals), span = max - min || 1;
  const y = v => 150 - (v - min) / span * 110;
  for (const v of [min, max]) { const text = svgElement("text", {x:10, y:y(v)-8, class:"chart-label"}); text.textContent = v.toLocaleString("fr-FR", {maximumFractionDigits:2}) + " $"; svg.append(text); }
  // Real timestamp spacing preserves gaps in sparse minute-bar histories.
  const times = bars.map(b => Date.parse(b.asof)), duration = times.at(-1) - times[0] || 1;
  const x = i => 15 + (times[i]-times[0]) / duration * 390;
  svg.append(svgElement("polyline", {points:vals.map((v,i)=>`${x(i)},${y(v)}`).join(" "),fill:"none",stroke:"#70e0c0","stroke-width":2.5}));
  bars.forEach((b,i) => {const dot = svgElement("circle",{cx:x(i),cy:y(Number(b.close)),r:4,fill:"#70e0c0"});const tip = svgElement("title",{});tip.textContent=`${b.asof} : ${b.close} USD`;dot.append(tip);svg.append(dot);});
  for (const i of [0,bars.length-1]) {const t = svgElement("text",{x:i===0?10:410,y:185,"text-anchor":i===0?"start":"end",class:"chart-label"});t.textContent=bars[i].asof.slice(11,16)+" UTC";svg.append(t);}
  return svg;
}
function render(data) {
  const cfg = data.configuration;
  ready = cfg.ai_credentials && Boolean(cfg.ai_model);
  const provider = cfg.ai_provider === "ollama" ? `Ollama local : ${cfg.ai_ready ? "modèle installé" : "serveur ou modèle indisponible"}` : `Hugging Face : ${cfg.ai_credentials ? "clé présente" : "clé manquante"}`;
  const modelLabel = cfg.role_models ? Object.entries(cfg.role_models).map(([role,model])=>`${role}: ${model}`).join(" / ") : cfg.ai_model;
  $("config").textContent = `Alpaca : ${cfg.market_credentials?"clés présentes":"clés manquantes"} · ${provider} · Modèle : ${modelLabel || "à configurer"}`;
  $("markets").replaceChildren();
  for (const asset of data.market.assets) {
    const card = node("article", undefined, "card"); card.append(node("h3", asset.symbol), node("p", labels[asset.status] || asset.status, asset.status));
    if (asset.session?.status === "closed") card.append(node("p", `Prochaine ouverture : ${new Date(asset.session.next_open).toLocaleString("fr-CA")}`, "muted"));
    if (asset.bars.length) {
      const last = asset.bars.at(-1); card.append(node("div", Number(last.close).toLocaleString("fr-FR",{style:"currency",currency:"USD"}),"price"),node("p",`${last.asof} · ${asset.feed}`,"muted"), chart(asset.bars,asset.symbol));
      const detail=node("details"),table=node("table"),head=node("tr");head.append(node("th","Date UTC"),node("th","Clôture USD"));table.append(head);
      for (const bar of asset.bars) {const row=node("tr");row.append(node("td",bar.asof),node("td",bar.close));table.append(row);}detail.append(node("summary","Voir les valeurs"),table);card.append(detail);
    } else card.append(node("p",asset.error || "Aucune observation."));
    $("markets").append(card);
  }
  $("roles").replaceChildren();
  const a = data.analysis;
  if (a) {
    const r=a.result;
    $("analysis-info").textContent=`${actions[r.action] || r.action} · ${r.code} · ${a.model} · ${a.analyzed_at}. Titres analysés : ${a.symbols_analyzed.join(", ")}. Exclus : ${a.symbols_excluded.join(", ") || "aucun"}. Exécution désactivée.`;
    for(const [role,record] of Object.entries(r.records)) {const card=node("article",undefined,"card");card.append(node("h3",({analyst:"Analyste",critic:"Critique",synthesizer:"Synthèse"})[role]||role),node("p",record.rationale || (record.concerns || []).join(" ")),node("p",`Sources : ${record.evidence_ids.join(", ")}`,"muted"));$("roles").append(card);}
    $("trace").textContent=JSON.stringify(a,null,2);
  } else {$("analysis-info").textContent="Aucune analyse effectuée. Les clés restent côté serveur.";$("trace").textContent="Aucun résultat.";}
  $("status").textContent=`Dernière acquisition : ${data.market.retrieved_at}. Les données anciennes restent exclues de l’analyse.`;
}
async function load(analysis=false) {
  if (busy) return; busy=true; $("refresh").disabled=true; $("analyze").disabled=true;
  $("status").textContent=analysis?"Analyse IA en cours (au plus trois appels)…":"Lecture Alpaca en cours…";
  try {
    const response=await fetch(analysis?"/api/analyze":"/api/market",analysis?{method:"POST",headers:{"X-CSRF-Token":document.querySelector('meta[name="csrf-token"]').content}}:{});
    const data=await response.json();if(!response.ok) throw new Error(data.error || "Requête refusée");render(data);
  } catch(error) {$("status").textContent=error.message;if(analysis){$("roles").replaceChildren();$("analysis-info").textContent="Cette demande n’a pas produit de nouvelle analyse.";$("trace").textContent="Aucun nouveau résultat.";}}
  finally {busy=false;$("refresh").disabled=false;$("analyze").disabled=!ready;}
}
$("refresh").addEventListener("click",()=>load());$("analyze").addEventListener("click",()=>load(true));load();
