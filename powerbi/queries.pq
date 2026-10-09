// Power Query (M) for the marching score model.
// 1) In Power BI Desktop: Home > Transform data > Manage Parameters > New
//      Name: BaseUrl   Type: Text
//      Value (GitHub option):  https://raw.githubusercontent.com/<you>/<repo>/main/data/
//      Value (OneDrive/local option): use the Folder/SharePoint version at the bottom instead.
// 2) New Source > Blank Query, open Advanced Editor, paste one query per table below.

// ---------- Scores (every score, all seasons, all classes)
let
    Source = Csv.Document(Web.Contents(BaseUrl & "scores.csv"), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    Typed = Table.TransformColumnTypes(Promoted, {
        {"season", Int64.Type}, {"event", type text}, {"event_date", type date}, {"round", type text},
        {"school", type text}, {"class", type text}, {"total", type number}, {"class_place", Int64.Type},
        {"source", type text}}, "en-US"),
    RoundOrder = Table.AddColumn(Typed, "round_order", each
        if [round] = "Prelims" then 1 else if [round] = "Finals" then 2 else if [round] = "Regional" then 3
        else if [round] = "State Semifinals" then 4 else if [round] = "State Finals" then 5 else 9, Int64.Type)
in
    RoundOrder

// ---------- WeeklySnapshot (last known score per band as of each Saturday)
let
    Source = Csv.Document(Web.Contents(BaseUrl & "weekly_snapshot.csv"), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    Typed = Table.TransformColumnTypes(Promoted, {
        {"week_ending", type date}, {"school", type text}, {"last_known_score", type number},
        {"event", type text}, {"event_date", type date}, {"rank_last_known", Int64.Type}}, "en-US")
in
    Typed

// ---------- Projections (projected semis score and odds)
let
    Source = Csv.Document(Web.Contents(BaseUrl & "projections.csv"), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    Typed = Table.TransformColumnTypes(Promoted, {
        {"school", type text}, {"region", type text}, {"basis", type text}, {"last_score", type number},
        {"last_event", type text}, {"last_date", type date}, {"proj_semis", type number}, {"proj_sd", type number},
        {"p_state", Percentage.Type}, {"p_finals", Percentage.Type}, {"p_first", Percentage.Type},
        {"median_semis_rank", type number}, {"p_state_if_one_fewer_spot", Percentage.Type}}, "en-US"),
    Low = Table.AddColumn(Typed, "proj_low", each [proj_semis] - [proj_sd], type number),
    High = Table.AddColumn(Low, "proj_high", each [proj_semis] + [proj_sd], type number)
in
    High

// ---------- FocusHistory (Standley Lake by season, aligned to State)
let
    Source = Csv.Document(Web.Contents(BaseUrl & "focus_history.csv"), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    Typed = Table.TransformColumnTypes(Promoted, {
        {"season", Int64.Type}, {"event", type text}, {"event_date", type date}, {"round", type text},
        {"total", type number}, {"days_before_state", Int64.Type}}, "en-US")
in
    Typed

// ---------- OneDrive / SharePoint alternative (no GitHub)
// Replace the Web.Contents(...) line in each query with:
//   SharePoint.Files("https://<tenant>-my.sharepoint.com/personal/<you>", [ApiVersion = 15]){[Name="scores.csv", #"Folder Path"="<full folder path>/data/"]}[Content]
// or for a local folder (needs the on-premises data gateway for scheduled refresh):
//   File.Contents("C:\Users\<you>\OneDrive\cba-scores\data\scores.csv")
