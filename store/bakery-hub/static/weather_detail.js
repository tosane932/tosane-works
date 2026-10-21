(() => {
    function renderForecast(data) {
        const today = document.getElementById("operations-weather-today");
        const week = document.getElementById("operations-weather-week");
        today.replaceChildren();
        week.replaceChildren();
        const rows = data.available && Array.isArray(data.forecast_days) ? data.forecast_days.slice(0, 7) : [];
        const status = document.getElementById("operations-weather-forecast-status");
        status.textContent = rows.length ? "" : "週間予報を取得できませんでした。";
        const currentDay = rows.find(row => row.date === (data.forecast_date || data.valid_at?.slice(0, 10)));
        const heading = document.createElement("h3");
        heading.textContent = "当日の予報";
        today.append(heading);
        if (currentDay) {
            const view = HomeWeather.day(currentDay);
            for (const text of [view.probability, view.temperature].filter(Boolean)) {
                const line = document.createElement("p");
                line.textContent = text;
                today.append(line);
            }
        } else {
            const note = document.createElement("p");
            note.textContent = "降水確率 —";
            today.append(note);
        }
        for (const row of rows) {
            const view = HomeWeather.day(row);
            const item = document.createElement("li");
            const date = document.createElement("time");
            date.dateTime = row.date;
            date.textContent = view.date;
            item.append(date);
            if (view.icon) {
                const source = document.querySelector('[data-weather-icon="' + view.icon + '"]');
                if (source) {
                    const icon = source.cloneNode(true);
                    icon.removeAttribute("hidden");
                    icon.removeAttribute("data-weather-icon");
                    icon.removeAttribute("data-icon");
                    icon.dataset.forecastIcon = view.icon;
                    item.append(icon);
                }
            }
            for (const text of [view.weather, view.probability, view.temperature,
                ...(view.location && view.location !== data.location ? [view.location] : [])].filter(Boolean)) {
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
            const response = await fetch("/api/home/weather", {credentials:"same-origin"});
            if (!response.ok) throw new Error("weather");
            const data = await response.json();
            status.textContent = data.available ? (data.weather_label || HomeWeather.condition(data.weather_code)) : data.reason;
            renderForecast(data);
            document.getElementById("operations-weather-location").textContent = data.available ? data.location : "";
            document.getElementById("operations-weather-source").textContent = "";
            document.getElementById("operations-weather-time").textContent = data.available ? "発表 " + HomeWeather.timestamp(data.valid_at) : "";
            const credit = document.getElementById("operations-weather-credit");
            credit.hidden = !data.available;
            if (data.available) {
                credit.href = data.credit_url;
                credit.textContent = data.source.startsWith("気象庁") ? "出典：気象庁ホームページ（予報を画面用に加工）" :
                    data.source.startsWith("MET Norway") ? "出典：MET Norway（天気区分・気温を加工／CC BY 4.0）" : "出典：Open-Meteo（表示用に加工）";
            }
            const icon = data.weather_icon || HomeWeather.icon(data.weather_code);
            document.getElementById("weather").dataset.condition = data.available ? (icon || "unknown") : "unknown";
            document.querySelectorAll("[data-weather-icon]").forEach(element => {
                element.toggleAttribute("hidden", !data.available || element.dataset.weatherIcon !== icon);
            });
        } catch {
            status.textContent = "現在の天気を取得できませんでした。";
            renderForecast({available:false});
            for (const id of ["location", "source", "time"]) document.getElementById("operations-weather-" + id).textContent = "";
            document.getElementById("operations-weather-credit").hidden = true;
            document.getElementById("weather").dataset.condition = "unknown";
            document.querySelectorAll("[data-weather-icon]").forEach(element => element.setAttribute("hidden", ""));
        }
    }
    refreshWeather();
})();
