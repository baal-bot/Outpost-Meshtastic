import {byId as $, safeLocalHref} from "/ui-primitives.js";

const query=new URLSearchParams(location.search),incidentId=Number(query.get("id")),handover=query.get("mode")==="handover",since=query.get("since");
const element=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined&&text!==null)node.textContent=String(text);return node;};
const time=value=>new Date(Number(value)*1000).toLocaleString();
const duration=milliseconds=>milliseconds?`${(Number(milliseconds)/1000).toFixed(2)}s`:"0.00s";

function fact(label,value,className=""){
  if(value===undefined||value===null||value==="")return null;
  return element("span",className,`${label}: ${value}`);
}

function renderEvent(event){
  const item=element("li","timeline-item"),stamp=element("time","timeline-time",time(event.timestamp)),category=element("span",`timeline-category ${event.category}`,event.category.replaceAll("_"," ")),body=element("div","timeline-body"),heading=element("h3","",event.title);
  stamp.dateTime=event.timestamp_iso;body.append(heading);
  if(event.actor)body.append(element("small","",event.actor));
  if(event.detail)body.append(element("p","",event.detail));
  const facts=element("div","timeline-facts"),values=[];
  if(event.category==="alert_stage"){
    values.push(fact("Stage",event.stage));
    values.push(fact("Addressed",event.addressed_count,event.zero_recipients?"failure":""));
    values.push(fact("Acknowledged",event.acknowledged_count));
    if(event.channels?.length)values.push(fact("Channels",event.channels.join(", ")));
    if(event.destinations?.length)values.push(fact("Audience",event.destinations.join(", ")));
  }
  if(event.category==="transmission"){
    values.push(fact("Destination",event.destination));values.push(fact("Channel",event.channel));values.push(fact("Outcome",event.outcome,/failed|uncertain|expired/.test(event.outcome)?"failure":""));values.push(fact("Actual ToA",event.actual_toa_ms==null?"not measured":`${event.actual_toa_ms} ms`));values.push(fact("Estimated ToA",event.estimated_toa_ms==null?null:`${event.estimated_toa_ms} ms`));
  }
  if(event.location)values.push(fact("Coarse location",`${event.location.lat}, ${event.location.lon}`));
  for(const value of values)if(value)facts.append(value);if(facts.children.length)body.append(facts);
  item.append(stamp,category,body);return item;
}

function meta(label,value){const wrapper=element("div"),term=element("dt","",label),description=element("dd","",value??"—");wrapper.append(term,description);return wrapper;}

function render(report){
  const incident=report.incident,summary=report.summary;
  document.title=`INC ${incident.local_ref} Record · Outpost`;
  $("report-title").textContent=`INC ${incident.local_ref} record`;
  $("incident-heading").textContent=incident.title;
  $("incident-detail").textContent=incident.body||incident.location_text||"No additional narrative recorded.";
  const metadata=$("incident-meta"),metadataItems=[meta("Status",incident.status.replaceAll("_"," ")),meta("Severity",incident.severity),meta("Reporter",incident.reporter),meta("Opened",time(incident.created_at))];if(incident.resolved_at)metadataItems.push(meta("Resolved",time(incident.resolved_at)));if(incident.resolution_note)metadataItems.push(meta("Resolution",incident.resolution_note));metadata.replaceChildren(...metadataItems);
  $("event-count").textContent=summary.window_event_count;$("event-total").textContent=`${summary.event_count} retained total`;$("stage-count").textContent=summary.alert_stage_count;$("zero-count").textContent=`${summary.zero_recipient_stages} reached nobody`;$("ack-count").textContent=summary.acknowledged_count;$("airtime-count").textContent=duration(summary.actual_airtime_ms);
  $("window-label").textContent=report.change_window.label;$("report-boundary").innerHTML="";const boundaryTitle=element("strong","",handover?"Shift-handover window.":"Complete retained record."),boundaryText=document.createTextNode(` ${report.change_window.label}. Delivery counts and measured airtime come from durable transmission evidence.`);$("report-boundary").append(boundaryTitle,boundaryText);
  const exportSuffix=report.change_window.since==null?"":`?since=${encodeURIComponent(report.change_window.since_iso)}`;$("csv-download").href=safeLocalHref(`/api/v1/incidents/${incidentId}/timeline.csv${exportSuffix}`);$("offline-download").href=safeLocalHref(`/api/v1/incidents/${incidentId}/offline.html${exportSuffix}`);
  const timeline=$("incident-timeline");timeline.replaceChildren();if(report.timeline.length)for(const event of report.timeline)timeline.append(renderEvent(event));else timeline.append(element("li","timeline-empty","No new evidence was recorded in this handover window."));
  $("report-generated").textContent=`Generated ${time(report.generated_at)} · coarse ${report.privacy.coarse_precision_m} m`;
}

async function load(){
  if(!Number.isInteger(incidentId)||incidentId<1){showError("A valid incident ID is required.");return;}
  const suffix=!handover&&since?`?since=${encodeURIComponent(since)}`:"",endpoint=handover?`/api/v1/incidents/${incidentId}/handover`:`/api/v1/incidents/${incidentId}/timeline${suffix}`;
  const switchQuery=new URLSearchParams({id:String(incidentId)});if(!handover)switchQuery.set("mode","handover");$("mode-switch").href=safeLocalHref(`/incident-report.html?${switchQuery}`);$("mode-switch").textContent=handover?"Complete record":"Shift handover";
  try{const response=await fetch(endpoint,{cache:"no-store"});if(response.status===401){location.href="/";return;}const body=await response.json();if(!response.ok)throw new Error(body.error?.message||"Incident report unavailable.");render(body);}catch(error){showError(error.message||"Incident report unavailable.");}
}

function showError(message){$("report-error").hidden=false;$("report-error").textContent=message;$("incident-timeline").replaceChildren();$("report-boundary").textContent=message;}

$("print-report").addEventListener("click",()=>window.print());load();
