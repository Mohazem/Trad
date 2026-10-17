"use strict";
const element=id=>document.getElementById(id);
const tabs=[...document.querySelectorAll("[data-strategy]")];
let data=null, selected="minute";
const labels={minute:"Minutes",swing:"Jours",portfolio:"Mois"};
function render() {
  if(!data)return;
  const row=data.strategies.find(s=>s.strategy_id===selected);
  tabs.forEach(t=>{const active=t.dataset.strategy===selected;t.setAttribute("aria-selected",String(active));t.tabIndex=active?0:-1;});
  element("strategy-panel").setAttribute("aria-labelledby",`tab-${selected}`);
  element("horizon").textContent=labels[selected];
  element("strategy-state").textContent=`Recherche ${row.enabled?"activée":"désactivée"} · exécution désactivée`;
  element("strategy-model").textContent=`Modèle candidat : ${row.candidate_model} · Version observée : ${row.model_version||"aucune"}`;
  element("strategy-budget").textContent=`Sous-allocation maximale : ${row.budget_ceiling_usd} USD, incluse dans le plafond global de 50 USD.`;
  element("strategy-data").textContent=`Dernière donnée : ${row.last_data||"indisponible"} · Provenance : ${row.data_kind||"aucune donnée importée"}`;
  element("strategy-reasons").textContent=`Alertes / abstentions : ${row.alerts.join(", ")||"aucune"}`;
  element("strategy-signal").textContent=row.last_signal?JSON.stringify(row.last_signal,null,2):"Aucun signal enregistré.";
  element("strategy-oos").textContent=row.out_of_sample_metrics?JSON.stringify(row.out_of_sample_metrics):"Aucune métrique ML hors échantillon disponible dans cette vue.";
  element("strategy-backtest").textContent=row.backtest_benchmarks?JSON.stringify(row.backtest_benchmarks,null,2):"Aucun benchmark chargé.";
}
tabs.forEach((tab,i)=>{
  tab.addEventListener("click",()=>{selected=tab.dataset.strategy;render();});
  tab.addEventListener("keydown",event=>{
    let next;
    if(event.key==="ArrowRight")next=(i+1)%tabs.length;
    if(event.key==="ArrowLeft")next=(i+tabs.length-1)%tabs.length;
    if(event.key==="Home")next=0;
    if(event.key==="End")next=tabs.length-1;
    if(next!==undefined){event.preventDefault();tabs[next].focus();tabs[next].click();}
  });
});
fetch("/api/strategies").then(async response=>{if(!response.ok)throw new Error("État indisponible");return response.json();}).then(result=>{
  data=result;
  element("paper").textContent="Moteur Paper indisponible · désarmé · positions et budget restant inconnus. Bornes : 50 USD au total, 45 USD par achat, une position.";
  element("alerts").textContent=result.alerts.join(" ");render();
}).catch(()=>{element("alerts").textContent="Impossible de lire l’état des stratégies. Aucune exécution.";});
