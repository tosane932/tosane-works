(() => {
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
            const response = await fetch("/api/home/weather", {credentials:"same-origin"});
            if (!response.ok) throw new Error("weather");
            const data = await response.json();
            status.textContent = data.available ? HomeWeather.condition(data.weather_code) : data.reason;
            renderForecast(data);
            document.getElementById("operations-weather-location").textContent = data.available ? data.location : "";
            document.getElementById("operations-weather-source").textContent = data.available ? data.source + "（観測記録ではありません）" : "過去の天気記録は現在の天気に利用しません。";
            document.getElementById("operations-weather-time").textContent = data.available ? "予報対象 " + HomeWeather.timestamp(data.valid_at) + " ／取得 " + HomeWeather.timestamp(data.fetched_at) : "";
            const credit = document.getElementById("operations-weather-credit");
            credit.hidden = !data.available;
            if (data.available) {
                credit.href = data.credit_url;
                credit.textContent = data.source.startsWith("MET Norway") ? "気象データ：MET Norway（天気区分・気温を集計・CC BY 4.0）" : "気象データ：Open-Meteo";
            }
            document.getElementById("weather").dataset.condition = data.available ? (HomeWeather.icon(data.weather_code) || "unknown") : "unknown";
            document.querySelectorAll("[data-weather-icon]").forEach(element => {
                element.toggleAttribute("hidden", !data.available || element.dataset.weatherIcon !== HomeWeather.icon(data.weather_code));
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
