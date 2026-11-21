"use strict";

const byId = id => document.getElementById(id);
const phaseButtons = [...document.querySelectorAll("[data-phase]")].filter(element => element.tagName === "BUTTON");
const phaseNames = { arrival: "Заселение", evening: "Вечер", night: "Ночь", dawn: "Рассвет", wake: "Подъём" };
const deviceNames = { light: "Свет", climate: "Климат", curtains: "Шторы", sound: "Звук", alarm: "Будильник", dnd: "Не беспокоить" };
const alarmModes = { light: "свет", sound: "звук", both: "свет и звук" };
const localeNumber = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
let room = null;
let waiting = false;
let stage = "dev";
let timelineKey = "";
let timelineItems = [];
let voiceAvailable = false;
let demoAvailable = true;
let recorder = null;
let playback = null;

function showNotice(text, kind = "normal") {
  const notice = byId("notice");
  notice.hidden = !text;
  notice.textContent = text;
  notice.dataset.kind = kind;
}

async function api(path, method = "GET", body) {
  const options = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => null);
  if (!response.ok || !data) {
    const error = new Error((typeof data?.detail === "string" ? data.detail : data?.error) || "Сервис номера недоступен. Попробуйте ещё раз.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function clockText(at) {
  return typeof at === "string" ? at.slice(11, 16) : "";
}

function stat(label, value, detail) {
  const element = document.createElement("div");
  element.className = "stat";
  const name = document.createElement("dt");
  const content = document.createElement("dd");
  name.textContent = label;
  content.textContent = value;
  if (detail) {
    const description = document.createElement("small");
    description.textContent = detail;
    content.append(description);
  }
  element.append(name, content);
  return element;
}

function formatDevice(device, values) {
  switch (device) {
    case "light":
      return [
        values.brightness === undefined ? "Яркость по сценарию" : values.brightness ? values.brightness + "%" : "Выключен",
        values.color_temperature_k ? values.color_temperature_k + " К" : "",
      ];
    case "climate":
      return [values.temperature_c == null ? "По сценарию" : localeNumber.format(values.temperature_c) + " °C", ""];
    case "curtains":
      return [values.position == null ? "По сценарию" : values.position === 0 ? "Закрыты" : values.position === 100 ? "Открыты" : values.position + "% открыто", ""];
    case "sound": {
      const track = room?.tracks?.find(item => item.track_id === values.track_id);
      const detail = [track?.title, values.timer_minutes > 0 ? "таймер " + values.timer_minutes + " мин" : ""].filter(Boolean).join(" · ");
      return [values.volume ? values.volume + "%" : "Тихо", values.volume ? detail : ""];
    }
    case "alarm":
      return [values.at ? clockText(values.at) : "Выключен", values.at ? alarmModes[values.mode] || "" : ""];
    case "dnd":
      return [values.enabled ? "Включено" : "Выключено", ""];
    default:
      return ["", ""];
  }
}

function renderDevices() {
  const list = byId("devices");
  list.hidden = !room;
  byId("devices-empty").hidden = !!room;
  if (!room) return;
  byId("room-visual").style.setProperty("--light-level", String((room.state.light?.brightness ?? 0) / 100));
  list.replaceChildren(...Object.entries(deviceNames).flatMap(([device, label]) => {
    return room.state[device] ? [stat(label, ...formatDevice(device, room.state[device]))] : [];
  }));
}

function phasesForStay() {
  if (!room) return {};
  const schedule = room.scenario;
  const night = room.bedtime || schedule.find(step => step.device_id === "light" && step.values.brightness === 0)?.at;
  const evening = schedule.find(step => step.device_id === "light" && step.values.brightness > 0 && (!night || Date.parse(step.at) < Date.parse(night)))?.at;
  const dawn = schedule.find(step => {
    if (!night || Date.parse(step.at) <= Date.parse(night)) return false;
    return (step.device_id === "light" && step.values.brightness > 0)
      || (step.device_id === "curtains" && step.values.position > 0);
  })?.at;
  return { evening: evening || room.check_in, night, dawn, wake: room.wake };
}

function currentPhase(phases) {
  let phase = "arrival";
  const current = Date.parse(room.at);
  for (const [key, at] of Object.entries(phases)) {
    if (at && Date.parse(at) <= current) phase = key;
  }
  return phase;
}

function captionForPhase(phase) {
  if (room.checkout) return "Заезд завершён";
  if (phase === "arrival") return "Заселение";
  if (phase === "evening") return "Подготовка ко сну";
  if (phase === "night") return "Ночной режим";
  if (phase === "dawn") return "Мягкое пробуждение";
  return "Время подъёма";
}

function describeStep(step) {
  const { device_id: device, values } = step;
  const [value, detail] = formatDevice(device, values);
  if (!deviceNames[device]) return "";
  return [deviceNames[device] + " — " + value, detail].filter(Boolean).join(", ");
}

function renderTimeline(phases) {
  const timeline = byId("timeline");
  const key = JSON.stringify(room.scenario);
  if (key !== timelineKey) {
    timelineKey = key;
    const groups = new Map();
    for (const step of room.scenario) {
      if (!groups.has(step.at)) groups.set(step.at, []);
      groups.get(step.at).push(step);
    }
    timelineItems = [...groups].sort(([a], [b]) => Date.parse(a) - Date.parse(b)).map(([at, steps]) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.at = at;
      button.textContent = clockText(at);
      button.setAttribute("aria-label", "Показать состояние в " + clockText(at));
      const content = document.createElement("div");
      const heading = document.createElement("h3");
      const phase = Object.keys(phases).find(key => Date.parse(phases[key]) === Date.parse(at));
      heading.textContent = phase ? phaseNames[phase] : Date.parse(at) === Date.parse(room.check_in) ? "Настройки при заселении" : "Следующий шаг пробуждения";
      const description = document.createElement("p");
      description.textContent = steps.map(describeStep).filter(Boolean).join(" · ");
      content.append(heading, description);
      item.append(button, content);
      return { item, button, at };
    });
    timeline.replaceChildren(...timelineItems.map(entry => entry.item));
  }
  const current = timelineItems.findLast(entry => Date.parse(entry.at) <= Date.parse(room.at));
  for (const entry of timelineItems) {
    entry.item.dataset.active = String(entry === current);
    if (entry === current) entry.button.setAttribute("aria-current", "time");
    else entry.button.removeAttribute("aria-current");
  }
  timeline.hidden = timelineItems.length === 0;
  byId("routine-empty").hidden = timelineItems.length !== 0;
}

function renderRecipe() {
  const checkout = room?.checkout;
  byId("recipe").hidden = !checkout;
  byId("return-actions").hidden = !checkout;
  byId("checkout").hidden = !!checkout;
  byId("home-caption").textContent = checkout
    ? "Настройки этого заезда, которые можно повторить дома."
    : "На выезде соберём настройки номера, которые можно повторить дома.";
  if (!checkout) return;
  const labels = { light: "Свет перед сном", climate: "Температура ночью", sound: "Фоновый звук", alarm: "Пробуждение", curtains: "Шторы", dnd: "Тишина" };
  const items = Object.entries(labels).flatMap(([device, label]) => {
    const settings = checkout.settings[device];
    if (!settings) return [];
    const [value, detail] = formatDevice(device, settings);
    const note = checkout.changed.includes(device) ? [detail, "Учтена просьба в чате"].filter(Boolean).join(" · ") : detail;
    return [stat(label, value, note)];
  });
  if (checkout.dawn_at) items.push(stat("Начало рассвета", clockText(checkout.dawn_at)));
  byId("recipe").replaceChildren(...items);
  byId("return-note").textContent = checkout.changed.length
    ? "Применим изменения к следующему заезду этого гостя."
    : "Следующий заезд начнётся по профилю гостя.";
}

function renderControls() {
  const active = !!room && !room.checkout;
  const locked = waiting || !!recorder || !active;
  const connection = byId("connection");
  connection.dataset.state = waiting ? "busy" : active ? "live" : "idle";
  connection.textContent = waiting ? "Обновляем номер" : active ? "Номер готов" : room ? "Заезд завершён" : "До заселения";
  byId("check-in").hidden = !!room || !demoAvailable;
  byId("check-in").disabled = waiting;
  byId("reset").hidden = !room;
  byId("reset").disabled = waiting || !!recorder;
  byId("checkout").disabled = locked;
  byId("stay-again").disabled = waiting || !room?.checkout;
  byId("virtual-time").disabled = locked;
  byId("apply-time").disabled = locked;
  byId("message").disabled = locked;
  byId("send").disabled = locked;
  byId("voice").disabled = !active || waiting;
  for (const button of byId("suggestions").children) button.disabled = locked;
  for (const entry of timelineItems) entry.button.disabled = locked;
  const phases = phasesForStay();
  for (const button of phaseButtons) {
    const at = phases[button.dataset.phase];
    button.disabled = locked || !at;
    button.querySelector("time").textContent = clockText(at);
    button.setAttribute("aria-pressed", String(!!room && button.dataset.phase === currentPhase(phases)));
  }
}

function updateRoom(data, newStay = false) {
  const reloaded = room && room.expires_at !== data.expires_at;
  room = data;
  byId("main").dataset.ready = "";
  if (newStay || reloaded) {
    timelineKey = "";
    byId("history").replaceChildren();
    addBubble(data.preferences_reused ? "Номер готов. Сохранённые настройки применены к новому заезду." : "Номер готов. Что хотите изменить?");
  }
  const phases = phasesForStay();
  const phase = currentPhase(phases);
  byId("room-visual").dataset.phase = phase;
  byId("phase-caption").textContent = captionForPhase(phase);
  byId("room-clock").hidden = false;
  byId("clock").textContent = clockText(room.at);
  byId("clock").dateTime = room.at;
  byId("custom-time").hidden = !!room.checkout;
  const input = byId("virtual-time");
  input.min = room.check_in.slice(0, 19);
  input.max = room.wake.slice(0, 19);
  input.value = room.at.slice(0, 19);
  byId("build-time").hidden = !Number.isFinite(room.scenario_ms);
  if (Number.isFinite(room.scenario_ms)) {
    byId("build-time").textContent = "План сна за " + (room.scenario_ms < 1 ? "< 1" : localeNumber.format(room.scenario_ms)) + " мс";
  }
  byId("routine-caption").textContent = room.preferences_reused
    ? "Учтены настройки, сохранённые при выезде."
    : "План составлен по профилю гостя и времени заезда.";
  if (room.stage) setStage(room.stage);
  renderDevices();
  renderTimeline(phases);
  renderRecipe();
  renderControls();
}

function setStage(value) {
  stage = value;
  byId("stage-label").textContent = stage === "final" ? "Финальный сеанс" : "Репетиция · симулятор";
}

function addBubble(text, kind = "reply") {
  const element = document.createElement("p");
  element.className = kind === "guest" ? "bubble guest" : "bubble";
  element.dataset.kind = kind;
  element.textContent = text;
  const history = byId("history");
  history.append(element);
  history.scrollTop = history.scrollHeight;
  return element;
}

async function act(operation) {
  if (waiting || recorder) return;
  const focused = document.activeElement;
  waiting = true;
  showNotice("");
  renderControls();
  try {
    await operation();
  } catch (error) {
    showNotice(error.message, "error");
  } finally {
    waiting = false;
    renderControls();
    if (document.activeElement === document.body && focused && !focused.disabled && focused.getClientRects().length) {
      focused.focus({ preventScroll: true });
    }
  }
}

function startStay(mode) {
  return act(async () => {
    const reset = mode !== "first";
    const data = await api(reset ? "/api/demo/reset" : "/api/demo", "POST", reset ? { preserve_preferences: mode === "repeat" } : undefined);
    updateRoom(data, true);
    byId("room-heading").tabIndex = -1;
    byId("room-heading").focus({ preventScroll: true });
    if (mode === "repeat") {
      showNotice(data.preferences_reused ? "Настройки перенесены в новый заезд." : "Новый номер подготовлен по профилю гостя.");
      byId("room").scrollIntoView();
    }
  });
}

function changeTime(at) {
  if (!room || room.checkout || !at) return;
  return act(async () => {
    updateRoom(await api("/api/room?" + new URLSearchParams({ at })));
  });
}

function virtualInstant(wallTime) {
  const local = wallTime.length === 16 ? wallTime + ":00" : wallTime;
  const offset = value => value?.match(/(Z|[+-]\d{2}:\d{2})$/)?.[0];
  const choices = new Set([room.at, room.check_in, room.wake, ...room.scenario.map(step => step.at)].map(offset).filter(Boolean));
  const formatter = room.hotel_timezone ? new Intl.DateTimeFormat("sv-SE", {
    timeZone: room.hotel_timezone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }) : null;
  for (const choice of choices) {
    const instant = local + choice;
    const date = new Date(instant);
    if (date < new Date(room.check_in) || date > new Date(room.wake)) continue;
    if (!formatter || formatter.format(date).replace(" ", "T") === local) return instant;
  }
  return null;
}

async function sendMessage(rawText) {
  const text = rawText.trim();
  if (waiting || !room || room.checkout || !text) return;
  byId("message").value = "";
  addBubble(text, "guest");
  const pending = addBubble("Обрабатываю просьбу…", "pending");
  await act(async () => {
    try {
      const data = await api("/api/message", "POST", { text });
      if (data.room) updateRoom(data.room);
      else {
        room.state = data.state;
        renderDevices();
      }
      pending.dataset.kind = "reply";
      pending.textContent = data.reply;
      if (data.actions?.length) {
        const detail = document.createElement("small");
        const devices = [...new Set(data.actions.map(action => deviceNames[action.device_id]).filter(Boolean))];
        detail.textContent = devices.length
          ? (data.response_class === "refused" ? "Частично изменено: " : "Изменено: ") + devices.join(", ")
          : "";
        if (detail.textContent) pending.append(detail);
      }
      if (voiceAvailable) {
        const listen = document.createElement("button");
        listen.type = "button";
        listen.className = "listen";
        listen.textContent = "Прослушать";
        listen.addEventListener("click", () => playReply(data.reply, listen));
        pending.append(listen);
      }
    } catch (error) {
      pending.dataset.kind = "error";
      pending.textContent = error.message;
      byId("message").value = text;
    }
    byId("history").scrollTop = byId("history").scrollHeight;
  });
}

byId("check-in").addEventListener("click", () => startStay("first"));
byId("stay-again").addEventListener("click", () => startStay("repeat"));
byId("reset").addEventListener("click", () => {
  byId("reset-dialog").returnValue = "";
  byId("reset-dialog").showModal();
});
byId("reset-dialog").addEventListener("close", event => {
  if (event.target.returnValue === "reset") startStay("reset");
});
byId("checkout").addEventListener("click", () => act(async () => {
  room.checkout = await api("/api/checkout", "POST");
  updateRoom(room);
  byId("home-heading").tabIndex = -1;
  byId("home-heading").focus();
}));
for (const button of phaseButtons) {
  button.addEventListener("click", () => changeTime(phasesForStay()[button.dataset.phase]));
}
byId("timeline").addEventListener("click", event => {
  const at = event.target.closest("button[data-at]")?.dataset.at;
  if (at) changeTime(at);
});
byId("time-form").addEventListener("submit", event => {
  event.preventDefault();
  const at = virtualInstant(byId("virtual-time").value);
  if (at) changeTime(at);
  else showNotice("Выберите существующее местное время между заселением и подъёмом.", "error");
});
byId("message-form").addEventListener("submit", event => {
  event.preventDefault();
  sendMessage(byId("message").value);
});
byId("suggestions").addEventListener("click", event => {
  if (event.target.tagName === "BUTTON") sendMessage(event.target.textContent);
});

async function playReply(text, button) {
  button.disabled = true;
  try {
    if (playback) playback.pause();
    const response = await fetch("/api/voice/speech", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!response.ok) throw new Error("Озвучивание недоступно. Ответ остался в чате.");
    const url = URL.createObjectURL(await response.blob());
    const audio = new Audio(url);
    playback = audio;
    const release = () => URL.revokeObjectURL(url);
    audio.addEventListener("ended", release, { once: true });
    audio.addEventListener("pause", release, { once: true });
    try {
      await audio.play();
    } catch (error) {
      release();
      throw error;
    }
  } catch (error) {
    showNotice(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function toggleRecording() {
  if (recorder) {
    if (recorder.state === "recording") recorder.stop();
    return;
  }
  const button = byId("voice");
  await act(async () => {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const chunks = [];
      const recording = new MediaRecorder(stream);
      recorder = recording;
      recording.addEventListener("dataavailable", event => {
        if (event.data.size) chunks.push(event.data);
      });
      const timer = setTimeout(() => {
        if (recording.state === "recording") recording.stop();
      }, 18000);
      recording.addEventListener("stop", () => {
        clearTimeout(timer);
        stream.getTracks().forEach(track => track.stop());
        recorder = null;
        button.textContent = "Голос";
        button.setAttribute("aria-pressed", "false");
        const audio = new Blob(chunks, { type: recording.mimeType || chunks[0]?.type });
        act(async () => {
          if (!audio.size) throw new Error("Запись пуста. Попробуйте ещё раз.");
          const response = await fetch("/api/voice/transcribe", { method: "POST", body: audio });
          const data = await response.json().catch(() => null);
          if (!response.ok || !data?.text) throw new Error(data?.detail || "Не удалось распознать запись.");
          byId("message").value = data.text;
          showNotice("Проверьте текст перед отправкой.");
        }).then(() => byId("message").focus());
      }, { once: true });
      recording.start();
      button.textContent = "Остановить";
      button.setAttribute("aria-pressed", "true");
    } catch (error) {
      stream?.getTracks().forEach(track => track.stop());
      recorder = null;
      throw new Error("Микрофон недоступен. Можно написать просьбу в чате.");
    }
  });
}

byId("voice").addEventListener("click", toggleRecording);
renderControls();
Promise.allSettled([api("/api/config"), api("/api/room")]).then(([config, restored]) => {
  if (config.status === "fulfilled") {
    setStage(config.value.stage);
    if (config.value.demo === false) {
      showNotice("Общий номер отключён в настройках сервера.");
      demoAvailable = false;
      renderControls();
    }
    voiceAvailable = !!config.value.voice;
    byId("voice").hidden = !(voiceAvailable && window.MediaRecorder && navigator.mediaDevices?.getUserMedia);
  }
  if (restored.status === "fulfilled" && !room && !waiting) updateRoom(restored.value, true);
});
