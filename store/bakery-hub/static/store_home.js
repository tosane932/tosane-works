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
            upcoming.textContent = "予定なし";
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

    function renderForecast(data) {
        const today = document.getElementById("operations-weather-today");
        const week = document.getElementById("operations-weather-week");
        today.replaceChildren();
        week.replaceChildren();
        const rows = data.available && Array.isArray(data.forecast_days) ? data.forecast_days.slice(0, 7) : [];
        const status = document.getElementById("operations-weather-forecast-status");
        status.textContent = rows.length ?
            "予報です。気温は記載の時間帯の予想値。時間帯限定の値は全日の最高・最低ではありません。天気アイコンは代表時刻の予報（Open-Meteoは日別予報）です。" :
            "当日詳細・週間予報は取得できませんでした。未提供の値は補完しません。";
        const currentDay = rows.find(row => row.date === data.valid_at?.slice(0, 10));
        const heading = document.createElement("h3");
        heading.textContent = "当日の予報";
        today.append(heading);
        if (currentDay) {
            const view = HomeWeather.day(currentDay);
            for (const text of [view.temperature, view.probability, view.coverage]) {
                const line = document.createElement("p");
                line.textContent = text;
                today.append(line);
            }
        } else {
            const note = document.createElement("p");
            note.textContent = "予想最高・最低気温、降水確率：未提供";
            today.append(note);
        }
        for (const row of rows) {
            const view = HomeWeather.day(row);
            const item = document.createElement("li");
            const date = document.createElement("time");
            date.dateTime = row.date;
            date.textContent = view.date;
            item.append(date);
            if (HomeWeather.icon(row.weather_code)) {
                const source = document.querySelector('[data-weather-icon="' + HomeWeather.icon(row.weather_code) + '"]');
                if (source) {
                    const icon = source.cloneNode(true);
                    icon.removeAttribute("hidden");
                    icon.removeAttribute("data-weather-icon");
                    icon.removeAttribute("data-icon");
                    item.append(icon);
                }
            }
            for (const text of [view.weather, view.temperature, view.probability, view.coverage, view.basis,
                ...(row.representative_at ? ["天気代表 " + row.representative_at.slice(11,16)] : [])]) {
                const line = document.createElement("span");
                line.textContent = text;
                item.append(line);
            }
            week.append(item);
        }
    }

    async function refreshWeather() {
        const status = document.getElementById("operations-weather-status");
        try {
            const response = await fetch("/api/home/weather", {credentials: "same-origin"});
            if (!response.ok) throw new Error("weather");
            const data = await response.json();
            status.textContent = data.available ? data.weather : data.reason;
            renderForecast(data);
            if (data.available && data.source === "MET Norway 予報") {
                // The server reads its validated forecast again; no weather content is sent by the browser.
                fetch("/api/home/weather/record", {
                    method: "POST", credentials: "same-origin",
                    headers: {"X-CSRFToken": document.querySelector('meta[name="csrf-token"]').content},
                }).catch(() => {});
            }
            document.getElementById("operations-weather-location").textContent =
                data.available ? data.location : "";
            document.getElementById("operations-weather-source").textContent =
                data.available ? data.source + "（観測記録ではありません）" : "過去の天気記録は現在の天気に利用しません。";
            document.getElementById("operations-weather-time").textContent =
                data.available ? "予報対象 " + HomeWeather.timestamp(data.valid_at) + " ／取得 " + HomeWeather.timestamp(data.fetched_at) : "";
            const credit = document.getElementById("operations-weather-credit");
            credit.hidden = !data.available;
            if (data.available) {
                credit.href = data.credit_url;
                credit.textContent = data.source.startsWith("MET Norway") ?
                    "気象データ：MET Norway（天気区分・気温を集計・CC BY 4.0）" : "気象データ：Open-Meteo";
            }
            const icon = HomeWeather.icon(data.weather_code);
            document.querySelectorAll("[data-weather-icon]").forEach(element => {
                if (data.available && element.dataset.weatherIcon === icon) {
                    element.removeAttribute("hidden");
                } else {
                    element.setAttribute("hidden", "");
                }
            });
        } catch {
            status.textContent = "現在の天気を取得できませんでした。";
            renderForecast({available:false});
            for (const id of ["location", "source", "time"]) document.getElementById("operations-weather-" + id).textContent = "";
            document.getElementById("operations-weather-credit").hidden = true;
            document.querySelectorAll("[data-weather-icon]").forEach(element => element.setAttribute("hidden", ""));
        }
    }

    async function loadBakeryNews() {
        const status = document.getElementById("bakery-news-status");
        const list = document.getElementById("bakery-news-list");
        if (!status || !list) return;
        try {
            const response = await fetch("/api/bakery-news", {credentials: "same-origin"});
            if (!response.ok) throw new Error("news");
            const data = await response.json();
            list.replaceChildren();
            for (const article of data.articles) {
                const item = document.createElement("li");
                const link = document.createElement("a");
                link.href = article.url;
                link.target = "_blank";
                link.rel = "noopener noreferrer";
                link.textContent = article.title;
                const date = document.createElement("time");
                date.dateTime = article.published;
                date.textContent = new Intl.DateTimeFormat("ja-JP", {
                    timeZone: "Asia/Tokyo", year: "numeric", month: "numeric", day: "numeric",
                }).format(new Date(article.published));
                item.append(link, date);
                list.append(item);
            }
            status.textContent = data.articles.length ? "" : "最近の関連する記事はありません。";
        } catch {
            status.textContent = "記事を取得できませんでした。提供元のサイトをご確認ください。";
        }
    }

    renderClock();
    refreshWeather();
    loadBakeryNews();
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
