// Composer の試験。ime/build.ps1 -Test がビルドして実行する（失敗があれば終了コード 1）。
using System;
using System.Collections.Generic;
using System.Linq;

namespace RomajiSlm
{
    static class ComposerTests
    {
        static int failures;
        static readonly IntPtr W1 = new IntPtr(100), W2 = new IntPtr(200);

        static void Check(bool ok, string what)
        {
            if (!ok) { failures++; Console.WriteLine("FAIL: " + what); }
        }

        static Composer Ready()
        {
            var c = new Composer();
            c.SetServerReady(true, 0);
            c.OnForeground(W1, 0);
            c.SetEnabled(true, 0);
            c.TakeEffects();
            return c;
        }

        static int VkFor(char ch)
        {
            if (ch >= 'a' && ch <= 'z') return 0x41 + (ch - 'a');
            if (ch >= '0' && ch <= '9') return 0x30 + (ch - '0');
            if (ch == ',') return 0xBC;
            if (ch == '.') return 0xBE;
            throw new ArgumentException(ch.ToString());
        }

        static List<bool> Type(Composer c, string s, long now)
        {
            var r = new List<bool>();
            foreach (char ch in s)
            {
                r.Add(c.OnKey(VkFor(ch), true, Mods.None, now, W1));
                c.OnKey(VkFor(ch), false, Mods.None, now, W1);
            }
            return r;
        }

        static bool Press(Composer c, int vk, Mods m, long now, IntPtr? w = null)
        {
            bool s = c.OnKey(vk, true, m, now, w ?? W1);
            c.OnKey(vk, false, m, now, w ?? W1);
            return s;
        }

        /// 呼び出し側の役: 効果を取り出し、DeliverEffect があれば TakeReady → 送った（または失敗した）ことにする。
        /// 送ったものは "T:文" / "K:+vk" / "K:-vk" の列で返す（ReplayEffect の離鍵も "R:-vk" で入れる）
        sealed class Out { public List<Effect> Effects = new List<Effect>(); public List<string> Sent = new List<string>(); }

        static Out Drain(Composer c, long now, bool fail = false)
        {
            var o = new Out();
            for (int round = 0; round < 6; round++)
            {
                var e = c.TakeEffects();
                if (e.Count == 0) break;
                o.Effects.AddRange(e);
                foreach (var r in e.OfType<ReplayEffect>())
                    foreach (var k in r.Keys) o.Sent.Add("R:" + (k.Down ? "+" : "-") + k.Vk);
                if (!e.OfType<DeliverEffect>().Any()) continue;
                var ds = c.TakeReady(now);
                if (ds.Count == 0) continue;
                if (fail) { c.DeliveryFailed("送れませんでした", now); continue; }
                foreach (var d in ds) o.Sent.Add(d.IsKey ? "K:" + (d.Key.Down ? "+" : "-") + d.Key.Vk : "T:" + d.Text);
                c.Delivered(now);
            }
            return o;
        }

        static string Texts(Out o) { return string.Concat(o.Sent.Where(s => s.StartsWith("T:")).Select(s => s.Substring(2))); }
        static T Last<T>(List<Effect> e) where T : Effect { return e.OfType<T>().LastOrDefault(); }

        static void Run(string name, Action t)
        {
            try { t(); }
            catch (Exception ex) { failures++; Console.WriteLine("FAIL: " + name + ": " + ex); }
        }

        static RequestEffect Req(Out o) { return Last<RequestEffect>(o.Effects); }

        static int Main()
        {
            var ctrl = new Mods(false, true, false, false, false);
            var ctrlAlt = new Mods(false, true, true, false, false);

            Run("無効のときは何も握りつぶさない", () =>
            {
                var c = new Composer();
                c.OnForeground(W1, 0);
                Check(!Type(c, "abc", 0).Any(x => x), "disabled passes letters");
                Check(!Press(c, Composer.VK_SPACE, Mods.ShiftOnly, 0), "disabled passes Shift+Space");
            });

            Run("Ctrl+Alt+Space で有効・無効。無効にするとき日本語なら MS-IME を開く", () =>
            {
                var c = new Composer();
                c.OnForeground(W1, 0);
                c.TakeEffects();
                Check(Press(c, Composer.VK_SPACE, ctrlAlt, 0), "hotkey swallowed");
                var e = c.TakeEffects();
                Check(c.Mode == Mode.Japanese, "enabled -> Japanese");
                Check(Last<SetImeEffect>(e) != null && !Last<SetImeEffect>(e).Open, "closes MS-IME");
                Check(e.OfType<AltMaskEffect>().Any(), "alt mask");
                Press(c, Composer.VK_SPACE, ctrlAlt, 0);
                e = c.TakeEffects();
                Check(c.Mode == Mode.Disabled, "disabled");
                Check(Last<SetImeEffect>(e) != null && Last<SetImeEffect>(e).Open && Last<SetImeEffect>(e).Window == W1, "reopens MS-IME on W1");
            });

            Run("Shift+Space で日本語と英語を切り替える", () =>
            {
                var c = Ready();
                Check(Press(c, Composer.VK_SPACE, Mods.ShiftOnly, 0), "swallowed");
                Check(c.Mode == Mode.English, "english");
                Check(!Type(c, "abc", 0).Any(x => x), "english passes letters");
                Press(c, Composer.VK_SPACE, Mods.ShiftOnly, 0);
                Check(c.Mode == Mode.Japanese, "back to japanese");
            });

            Run("打鍵はためて、間を置くと変換を要求し、Enter で 1 位を送る", () =>
            {
                var c = Ready();
                Check(Type(c, "kyouha", 0).All(x => x), "letters swallowed");
                Check(c.Buffer == "kyouha", "buffer");
                c.Tick(50);
                Check(Req(Drain(c, 50)) == null, "no request before debounce");
                c.Tick(200);
                var req = Req(Drain(c, 200));
                Check(req != null && req.Romaji == "kyouha" && req.Context == "", "request");
                c.OnResult(req.Id, new[] { "今日は", "京は" }, null, 300);
                Drain(c, 300);
                Check(c.Candidates != null && c.Candidates[0] == "今日は", "preview");
                Check(Press(c, Composer.VK_RETURN, Mods.None, 400), "enter swallowed");
                var o = Drain(c, 400);
                Check(Texts(o) == "今日は", "sent first");
                Check(c.Context == "今日は", "context");
                Check(!c.Composing && !c.Waiting, "cleared");
            });

            Run("変換が済む前の Enter は、届いてから送る。その間に打った文字は次の文になる", () =>
            {
                var c = Ready();
                Type(c, "ame", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 10);
                var req = Req(Drain(c, 10));
                Check(req != null && req.Romaji == "ame", "request for commit");
                Type(c, "da", 20);
                Check(c.Buffer == "da", "next sentence");
                c.OnResult(req.Id, new[] { "雨" }, null, 300);
                var o = Drain(c, 300);
                Check(Texts(o) == "雨", "sent after result");
                req = Req(o);   // 続けて次の文の変換を要求する（打鍵から 120 ms 以上たっている）
                Check(req != null && req.Romaji == "da" && req.Context == "雨", "next request has context");
            });

            Run("Space で候補、もう一度で次候補、Enter でその候補", () =>
            {
                var c = Ready();
                Type(c, "kami", 0);
                c.Tick(200);
                var req = Req(Drain(c, 200));
                c.OnResult(req.Id, new[] { "紙", "髪", "神" }, null, 250);
                Press(c, Composer.VK_SPACE, Mods.None, 300);
                Check(c.Phase == Phase.Candidates && c.CandIndex == 0, "first space shows list at 1st");
                Press(c, Composer.VK_SPACE, Mods.None, 310);
                Press(c, Composer.VK_SPACE, Mods.None, 320);
                Check(c.CandIndex == 2, "third");
                Press(c, Composer.VK_UP, Mods.None, 330);
                Check(c.CandIndex == 1, "up");
                Drain(c, 330);
                Press(c, Composer.VK_RETURN, Mods.None, 340);
                Check(Texts(Drain(c, 340)) == "髪", "selected");
            });

            Run("候補の表示中に文字を打つと、その候補で確定して次の文を始める", () =>
            {
                var c = Ready();
                Type(c, "kami", 0);
                c.Tick(200);
                var req = Req(Drain(c, 200));
                c.OnResult(req.Id, new[] { "紙", "髪" }, null, 250);
                Press(c, Composer.VK_SPACE, Mods.None, 300);
                Press(c, Composer.VK_SPACE, Mods.None, 300);
                Drain(c, 300);
                Type(c, "w", 310);
                Check(Texts(Drain(c, 310)) == "髪", "committed");
                Check(c.Buffer == "w", "new buffer");
            });

            Run("Esc: 候補表示なら戻る、打鍵中なら捨てる。Backspace は 1 字消す", () =>
            {
                var c = Ready();
                Type(c, "abc", 0);
                Press(c, Composer.VK_BACK, Mods.None, 1);
                Check(c.Buffer == "ab", "backspace");
                Press(c, Composer.VK_SPACE, Mods.None, 2);
                Check(c.Phase == Phase.Candidates, "cand");
                Press(c, Composer.VK_ESCAPE, Mods.None, 3);
                Check(c.Phase == Phase.Raw && c.Buffer == "ab", "esc back");
                Press(c, Composer.VK_ESCAPE, Mods.None, 4);
                Check(!c.Composing && c.Phase == Phase.Idle, "esc clear");
                Check(Texts(Drain(c, 5)) == "", "nothing sent");
            });

            Run("Shift+Enter は打鍵列のまま確定（サーバが無くても）。Enter はサーバが無ければ待ち行列に入れない", () =>
            {
                var c = new Composer();
                c.OnForeground(W1, 0);
                c.SetEnabled(true, 0);
                c.TakeEffects();
                Type(c, "jr", 0);
                c.Tick(500);
                Check(Req(Drain(c, 500)) == null, "no request while server not ready");
                Press(c, Composer.VK_RETURN, Mods.None, 550);
                Check(!c.Waiting && c.Buffer == "jr" && c.Status.Contains("Shift+Enter"), "enter does not queue");
                Check(!Press(c, Composer.VK_LEFT, Mods.None, 560) || c.Composing, "not stuck");
                Press(c, Composer.VK_RETURN, Mods.ShiftOnly, 600);
                Check(Texts(Drain(c, 600)) == "jr", "raw");
            });

            Run("変換に失敗したら、打鍵列を欄に戻して何も送らない", () =>
            {
                var c = Ready();
                Type(c, "abc", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                var req = Req(Drain(c, 1));
                c.OnResult(req.Id, null, "500", 50);
                var o = Drain(c, 50);
                Check(Texts(o) == "", "nothing sent");
                Check(c.Buffer == "abc" && c.Status.Contains("Shift+Enter"), "restored");
            });

            Run("時間切れでも打鍵列を欄に戻す", () =>
            {
                var c = Ready();
                Type(c, "abc", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                Drain(c, 1);
                c.Tick(1 + c.TimeoutMs + 1);
                Check(c.Buffer == "abc" && !c.Waiting, "restored after timeout");
            });

            Run("確定待ちの間にウィンドウが変わったら送らずに欄へ戻し、新しい窓で Enter すれば入る", () =>
            {
                var c = Ready();
                Type(c, "abc", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                var req = Req(Drain(c, 1));
                c.OnForeground(W2, 10);
                Check(c.Buffer == "abc", "restored into composition");
                c.OnResult(req.Id, new[] { "ABC" }, null, 50);
                Check(Texts(Drain(c, 50)) == "", "not sent to other window");
                Press(c, Composer.VK_RETURN, Mods.None, 60, W2);
                Check(Texts(Drain(c, 60)) == "ABC", "enter commits in the new window");
            });

            Run("Ctrl・Alt の組み合わせは素通し、未確定の文は残る", () =>
            {
                var c = Ready();
                Type(c, "ab", 0);
                Check(!Press(c, 0x43, ctrl, 1), "ctrl+c passes");
                Check(!Press(c, Composer.VK_TAB, new Mods(false, false, true, false, false), 1), "alt+tab passes");
                Check(c.Buffer == "ab", "kept");
            });

            Run("押下を握りつぶしたキーは離鍵も握りつぶす", () =>
            {
                var c = Ready();
                Check(c.OnKey(0x41, true, Mods.None, 0, W1), "down");
                Check(c.OnKey(0x41, false, Mods.None, 0, W1), "up swallowed");
                Check(!c.OnKey(0x70, false, Mods.None, 0, W1), "unrelated up passes");
            });

            Run("未確定でない Backspace は左文脈から 1 字消す", () =>
            {
                var c = Ready();
                Type(c, "a", 0);
                Press(c, Composer.VK_RETURN, Mods.ShiftOnly, 0);
                Drain(c, 0);
                Check(c.Context == "a", "ctx");
                Check(!Press(c, Composer.VK_BACK, Mods.None, 1), "passes");
                Check(c.Context == "", "ctx popped");
            });

            Run("記号: , . ? ! は打鍵列に入り、@ などは素通し", () =>
            {
                Check(Composer.CharFor(0xBF, Mods.ShiftOnly) == '?', "?");
                Check(Composer.CharFor(0x31, Mods.ShiftOnly) == '!', "!");
                Check(Composer.CharFor(0x32, Mods.ShiftOnly) == '\0', "@");
                Check(Composer.CharFor(0x41, new Mods(false, false, false, false, true)) == 'A', "caps");
                var c = Ready();
                Check(!Press(c, 0x32, Mods.ShiftOnly, 0), "@ passes when idle");
                Type(c, "a", 0);
                Check(Press(c, 0x32, Mods.ShiftOnly, 0), "@ ignored while composing");
                Check(c.Buffer == "a", "not added");
            });

            Run("パスワード欄: 確かめ終わるまで打鍵を預かり、パスワード欄ならそのままの順で送って以後は素通し", () =>
            {
                var c = Ready();
                c.RequireFieldCheck = true;
                Check(Type(c, "pa", 0).All(x => x), "held");
                var chk = Last<CheckFieldEffect>(Drain(c, 0).Effects);
                Check(chk != null && c.FieldPending && c.Buffer == "", "check requested, nothing composed");
                c.Tick(500);
                Check(Req(Drain(c, 500)) == null, "no request while checking");
                c.OnFieldChecked(chk.Seq, true, 510);
                Check(string.Join(",", Drain(c, 510).Sent) == "K:+80,K:-80,K:+65,K:-65", "raw keys in order");
                Check(!Type(c, "ss", 520).Any(x => x), "passthrough afterwards");
                Press(c, Composer.VK_TAB, Mods.None, 530);
                Check(Type(c, "a", 540).All(x => x), "tab leaves the field");
                chk = Last<CheckFieldEffect>(Drain(c, 540).Effects);
                c.OnFieldChecked(chk.Seq, false, 550);
                Check(c.Buffer == "a", "normal field: reprocessed into composition");
                c.Tick(800);
                Check(Req(Drain(c, 800)) != null, "normal field converts");
            });

            Run("パスワード欄: 確認の前に Enter しても次の文を打ち始めても、記号も含めて全部そのまま送る", () =>
            {
                var c = Ready();
                c.RequireFieldCheck = true;
                Type(c, "se", 0);
                var chk = Last<CheckFieldEffect>(Drain(c, 0).Effects);
                Press(c, 0x32, Mods.ShiftOnly, 1);   // @
                Press(c, Composer.VK_RETURN, Mods.None, 2);
                Type(c, "x", 3);
                Check(Last<CheckFieldEffect>(Drain(c, 3).Effects) == null, "no second check while pending");
                c.OnFieldChecked(chk.Seq, true, 20);
                var o = Drain(c, 20);
                Check(Req(o) == null && Texts(o) == "", "never converted");
                Check(string.Join(",", o.Sent) == "K:+83,K:-83,K:+69,K:-69,K:+50,K:-50,K:+13,K:-13,K:+88,K:-88", "all keys raw: " + string.Join(",", o.Sent));
                Check(c.Buffer == "" && !c.Waiting, "nothing left");
            });

            Run("欄の確認中に欄が替わったら、預かった打鍵は捨てて遅れた結果も無視する", () =>
            {
                var c = Ready();
                c.RequireFieldCheck = true;
                Type(c, "se", 0);
                var chk = Last<CheckFieldEffect>(Drain(c, 0).Effects);
                c.OnFocus("200:-4:1", 5);
                Check(!c.FieldPending && c.Buffer == "" && c.Status.Contains("捨て"), "dropped");
                c.OnFieldChecked(chk.Seq, true, 10);
                var o = Drain(c, 10);
                Check(o.Sent.All(s => s.StartsWith("R:-")) && Texts(o) == "", "late result ignored: " + string.Join(",", o.Sent));
                Check(Type(c, "a", 20).All(x => x) && Last<CheckFieldEffect>(Drain(c, 20).Effects) != null, "new field is checked again");
            });

            Run("普通の欄なら、預かった Enter も含めて処理し直す。確かめた欄では次から待たない", () =>
            {
                var c = Ready();
                c.RequireFieldCheck = true;
                c.OnFocus("100:-4:1", 0);
                Type(c, "ame", 0);
                var chk = Last<CheckFieldEffect>(Drain(c, 0).Effects);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                c.OnFieldChecked(chk.Seq, false, 30);
                var req = Req(Drain(c, 30));
                Check(req != null && req.Romaji == "ame" && c.PendingCommits == 1, "held enter commits after reprocessing");
                c.OnResult(req.Id, new[] { "雨" }, null, 300);
                Check(Texts(Drain(c, 300)) == "雨", "sent");
                Type(c, "b", 400);
                Check(Last<CheckFieldEffect>(Drain(c, 400).Effects) == null && c.Buffer == "b", "same field: no check");
            });

            Run("確定待ちの間の Enter は、確定文を送ったあとに送る", () =>
            {
                var c = Ready();
                Type(c, "ame", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                var req = Req(Drain(c, 1));
                Check(Press(c, Composer.VK_RETURN, Mods.None, 2), "second enter held back");
                c.OnResult(req.Id, new[] { "雨" }, null, 300);
                var o = Drain(c, 300);
                Check(string.Join(",", o.Sent) == "T:雨,K:+13,K:-13", "order: " + string.Join(",", o.Sent));
            });

            Run("変換済みの確定を送る前（Run の前）に届いた Enter も、確定文の後ろに並ぶ", () =>
            {
                var c = Ready();
                Type(c, "ame", 0);
                c.Tick(200);
                var req = Req(Drain(c, 200));
                c.OnResult(req.Id, new[] { "雨" }, null, 250);
                Drain(c, 250);
                Press(c, Composer.VK_RETURN, Mods.None, 300);   // 確定（DeliverEffect が積まれる）
                Check(Press(c, Composer.VK_RETURN, Mods.None, 301), "second enter before delivery is held");
                Check(string.Join(",", Drain(c, 302).Sent) == "T:雨,K:+13,K:-13", "order");
            });

            Run("確定待ちの間の Ctrl+C は、自前の修飾キーの状態で判定してまとめて送り直す", () =>
            {
                var c = Ready();
                Type(c, "a", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                var req = Req(Drain(c, 1));
                Check(c.OnKey(0xA2, true, ctrl, 2, W1), "ctrl down held");
                Check(c.OnKey(0x43, true, ctrl, 3, W1), "c held");
                Check(c.Buffer == "", "c is not composed");
                c.OnKey(0x43, false, ctrl, 4, W1);
                c.OnKey(0xA2, false, Mods.None, 5, W1);
                c.OnResult(req.Id, new[] { "あ" }, null, 300);
                Check(string.Join(",", Drain(c, 300).Sent) == "T:あ,K:+162,K:+67,K:-67,K:-162", "replayed ctrl+c");
            });

            Run("確定待ちの間に英字へ切り替えて打った文字も、確定文のあと", () =>
            {
                var c = Ready();
                Type(c, "ame", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                var req = Req(Drain(c, 1));
                Press(c, Composer.VK_SPACE, Mods.ShiftOnly, 2);
                Check(c.Mode == Mode.English, "english");
                Check(Type(c, "b", 3).All(x => x), "held back");
                c.OnResult(req.Id, new[] { "雨" }, null, 300);
                Check(string.Join(",", Drain(c, 300).Sent) == "T:雨,K:+66,K:-66", "order");
            });

            Run("確定の失敗: 待ち行列の確定を順に欄へ戻し、積んだ打鍵は離鍵だけ送る", () =>
            {
                var c = Ready();
                Type(c, "a", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                var req = Req(Drain(c, 1));
                Type(c, "b", 2);
                Press(c, Composer.VK_RETURN, Mods.None, 3);
                Press(c, Composer.VK_RETURN, Mods.None, 4);   // 積まれる
                c.OnResult(req.Id, null, "500", 50);
                var o = Drain(c, 50);
                Check(Texts(o) == "", "nothing sent");
                Check(c.Buffer == "ab", "restored in order: " + c.Buffer);
                Check(o.Sent.All(s => s.StartsWith("R:-")), "only key-ups replayed: " + string.Join(",", o.Sent));
                Check(!c.Waiting, "queue cleared");
            });

            Run("送信の失敗: 送ろうとした確定と後ろの確定を順に欄へ戻す", () =>
            {
                var c = Ready();
                Type(c, "a", 0);
                c.Tick(200);
                var req = Req(Drain(c, 200));
                c.OnResult(req.Id, new[] { "A" }, null, 210);
                Drain(c, 210);
                Press(c, Composer.VK_RETURN, Mods.None, 300);   // a は変換済み → 送る番
                Type(c, "b", 301);
                Press(c, Composer.VK_RETURN, Mods.None, 302);   // b は未変換
                Type(c, "c", 303);
                var o = Drain(c, 304, fail: true);
                Check(Texts(o) == "", "nothing sent");
                Check(c.Buffer == "abc" && !c.Waiting, "restored a, b, then c: " + c.Buffer);
            });

            Run("確定待ちのまま窓が替わったら、応答を待たずに順に戻す", () =>
            {
                var c = Ready();
                Type(c, "a", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                Type(c, "b", 2);
                Press(c, Composer.VK_RETURN, Mods.None, 3);
                c.OnForeground(W2, 4);
                Check(c.Buffer == "ab" && !c.Waiting, "restored ab: " + c.Buffer);
            });

            Run("確定待ちのままフォーカスの欄が替わったら戻す。送る前（Run の前）でも戻す。同じ欄の重ねての通知では戻さない", () =>
            {
                var c = Ready();
                c.OnFocus("100:-4:1", 0);
                Type(c, "a", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                c.OnFocus("100:-4:1", 2);
                Check(c.PendingCommits == 1, "same focus keeps");
                c.OnFocus("100:-4:2", 3);
                Check(c.Buffer == "a" && !c.Waiting, "other field restores");

                c.Tick(200);
                var req = Req(Drain(c, 200));
                c.OnResult(req.Id, new[] { "あ" }, null, 210);
                Drain(c, 210);
                Press(c, Composer.VK_RETURN, Mods.None, 300);   // 変換済み → DeliverEffect を積む
                c.OnFocus("100:-4:3", 301);                     // Run の前に欄が替わる
                Check(Texts(Drain(c, 302)) == "" && c.Buffer == "a", "not sent to the new field");
            });

            Run("キーリピート: 押下の判断を離鍵まで引き継ぐ", () =>
            {
                var c = Ready();
                Check(!c.OnKey(0x41, true, ctrl, 0, W1), "ctrl+a passes");
                Check(!c.OnKey(0x41, true, Mods.None, 1, W1), "repeat after ctrl released still passes");
                Check(!c.OnKey(0x41, false, Mods.None, 2, W1), "up passes");
                Check(!c.Composing, "nothing composed");
                Check(c.OnKey(0x42, true, Mods.None, 3, W1), "b swallowed");
                Check(c.OnKey(0x42, true, ctrl, 4, W1), "repeat with ctrl still swallowed");
                Check(c.OnKey(0x42, true, Mods.None, 5, W1), "plain repeat swallowed");
                Check(c.Buffer == "bb", "plain repeat appends, ctrl repeat does not: " + c.Buffer);
                Check(c.OnKey(0x42, false, Mods.None, 6, W1), "up swallowed");
            });

            Run("積んだ押下を捨てたら、その後のリピートと離鍵も渡さない", () =>
            {
                var c = Ready();
                Type(c, "a", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                Drain(c, 1);
                Check(c.OnKey(Composer.VK_RETURN, true, Mods.None, 2, W1), "second enter held");
                c.OnForeground(W2, 3);   // 戻す（押下は捨てる）
                Drain(c, 3);
                Check(c.OnKey(Composer.VK_RETURN, true, Mods.None, 4, W2), "repeat not leaked");
                Check(c.OnKey(Composer.VK_RETURN, false, Mods.None, 5, W2), "up not leaked");
            });

            Run("積んだ押下を送り直したら、その後の離鍵は渡す", () =>
            {
                var c = Ready();
                Type(c, "a", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                var req = Req(Drain(c, 1));
                Check(c.OnKey(Composer.VK_RIGHT, true, Mods.None, 2, W1), "held");
                c.OnResult(req.Id, new[] { "あ" }, null, 3);
                Check(string.Join(",", Drain(c, 3).Sent) == "T:あ,K:+39", "down replayed");
                Check(!c.OnKey(Composer.VK_RIGHT, false, Mods.None, 4, W1), "up passes");
            });

            Run("捨てた Enter を押し続けても、リピートで確定し直さない", () =>
            {
                var c = Ready();
                Type(c, "a", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                var req = Req(Drain(c, 1));
                c.OnKey(Composer.VK_RETURN, true, Mods.None, 2, W1);   // 積まれる
                c.OnForeground(W2, 3);                                 // 戻す: a は欄へ、Enter の押下は捨てる
                Drain(c, 3);
                Check(c.OnKey(Composer.VK_RETURN, true, Mods.None, 4, W2), "repeat swallowed");
                Check(c.Buffer == "a" && !c.Waiting, "repeat did not re-commit");
                c.OnResult(req.Id, new[] { "あ" }, null, 5);
                Check(Texts(Drain(c, 5)) == "", "nothing sent to W2");
            });

            Run("Shift+Space を押しっぱなしにしてもモードは 1 回だけ替わる", () =>
            {
                var c = Ready();
                c.OnKey(Composer.VK_SPACE, true, Mods.ShiftOnly, 0, W1);
                c.OnKey(Composer.VK_SPACE, true, Mods.ShiftOnly, 1, W1);
                c.OnKey(Composer.VK_SPACE, true, Mods.ShiftOnly, 2, W1);
                Check(c.OnKey(Composer.VK_SPACE, false, Mods.ShiftOnly, 3, W1), "up swallowed");
                Check(c.Mode == Mode.English, "toggled once");
            });

            Console.WriteLine(failures == 0 ? "all passed" : failures + " failure(s)");
            return failures == 0 ? 0 : 1;
        }
    }
}
