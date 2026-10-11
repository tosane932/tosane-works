"""過去の売上日とひもづく天気記録の表示補助。"""

def weather_category(code):
    if code == 0:
        return "晴れ"
    if code in (1, 2):
        return "晴れ・曇り"
    if code == 3:
        return "曇り"
    if code in (45, 48):
        return "霧"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "雨"
    if code in (71, 73, 75, 77, 85, 86):
        return "雪"
    if code in (95, 96, 99):
        return "雷雨"
    return "不明"
