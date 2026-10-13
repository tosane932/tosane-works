(() => {
    const raw = document.getElementById("operations-initial-schedule");
    if (!raw) return;
    let schedules = JSON.parse(raw.textContent);
    let day = document.getElementById("operations-date").dateTime;
    const clock = document.getElementById("operations-clock");
    const current = document.getElementById("operations-current");
    const upcoming = document.getElementById("operations-upcoming");
    const list = document.getElementById("operations-schedule-list");
    const japanClock = new Intl.DateTimeFormat("ja-JP", {
        timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    });

    function setScheduleList() {
        list.replaceChildren();
        if (!schedules.length) {
            const item = document.createElement("li");
            item.textContent = "今日の予定はありません。";
            list.append(item);
            return;
        }
        for (const row of schedules) {
            const item = document.createElement("li");
            const link = document.createElement("a");
            const time = document.createElement("time");
            link.href = row.url;
            time.dateTime = row.start;
            time.textContent = row.start.slice(11, 16);
            link.append(time, document.createTextNode("　" + row.title));
            item.append(link);
            list.append(item);
        }
    }

    function renderClock() {
        const now = new Date();
        clock.textContent = "現在 " + japanClock.format(now);
        const active = schedules.find(row =>
            row.end && new Date(row.start) <= now && now < new Date(row.end));
        const next = schedules.find(row => new Date(row.start) > now);
        current.replaceChildren();
        upcoming.replaceChildren();
        if (active) {
            const label = document.createElement("p");
            label.textContent = "現在の予定";
            const link = document.createElement("a");
            link.href = active.url;
            link.textContent = active.start.slice(11, 16) + "　" + active.title;
            current.append(label, link);
        }
        if (next) {
            const label = document.createElement("p");
            label.textContent = "次の予定";
            const link = document.createElement("a");
            link.href = next.url;
            link.textContent = next.start.slice(11, 16) + "　" + next.title;
            const remaining = Math.ceil((new Date(next.start) - now) / 60000);
            const note = document.createElement("small");
            note.textContent = "開始予定まで" + remaining + "分";
            upcoming.append(label, link, note);
        } else {
            upcoming.textContent = "次の予定はありません。";
        }
    }

    async function refreshSchedule() {
        try {
            const response = await fetch("/api/home/schedule", {credentials: "same-origin"});
            if (!response.ok) throw new Error("schedule");
            const data = await response.json();
            schedules = data.schedules;
            if (day !== data.date) {
                day = data.date;
                const dateLabel = document.getElementById("operations-date");
                dateLabel.dateTime = day;
                dateLabel.textContent = day.replace(/^(\d{4})-(\d{2})-(\d{2})$/, "$1年$2月$3日");
            }
            setScheduleList();
            renderClock();
        } catch {
            // Keep the last known list; the page remains usable without a network response.
        }
    }

    async function refreshWeather() {
        const status = document.getElementById("operations-weather-status");
        try {
            const response = await fetch("/api/home/weather", {credentials: "same-origin"});
            if (!response.ok) throw new Error("weather");
            const data = await response.json();
            status.textContent = data.available ? data.weather : data.reason;
            if (data.available && data.source === "MET Norway 予報") {
                // The server reads its validated forecast again; no weather content is sent by the browser.
                fetch("/api/home/weather/record", {
                    method: "POST", credentials: "same-origin",
                    headers: {"X-CSRFToken": document.querySelector('meta[name="csrf-token"]').content},
                }).catch(() => {});
            }
            document.getElementById("operations-weather-location").textContent =
                data.available ? "対象地点：" + data.location : "";
            document.getElementById("operations-weather-source").textContent =
                data.available ? data.source + "（観測記録ではありません）" : "過去の天気記録は現在の天気に利用しません。";
            document.getElementById("operations-weather-time").textContent =
                data.available ? "対象 " + data.valid_at.slice(11, 16) + " ／取得 " + data.fetched_at.slice(11, 16) : "";
            const credit = document.getElementById("operations-weather-credit");
            credit.hidden = !data.available;
            if (data.available) {
                credit.href = data.credit_url;
                credit.textContent = data.source.startsWith("MET Norway") ?
                    "気象データ：MET Norway（天気区分を要約・CC BY 4.0）" : "気象データ：Open-Meteo";
            }
            let icon = "cloud";
            if (data.weather_code === 0) icon = "sun";
            if ([51,53,55,56,57,61,63,65,66,67,80,81,82,95,96,99].includes(data.weather_code)) icon = "rain";
            if ([71,73,75,77,85,86].includes(data.weather_code)) icon = "snow";
            document.querySelectorAll("[data-weather-icon]").forEach(element => {
                element.hidden = !data.available || element.dataset.weatherIcon !== icon;
            });
        } catch {
            status.textContent = "現在の天気を取得できませんでした。";
        }
    }

    renderClock();
    refreshWeather();
    window.setInterval(() => {
        if (!document.hidden) {
            renderClock();
            refreshSchedule();
        }
    }, 60000);
    window.setInterval(() => { if (!document.hidden) refreshWeather(); }, 600000);
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) { refreshSchedule(); refreshWeather(); }
    });
})();
