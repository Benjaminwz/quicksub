// quicksub-cli.exe：快字幕指令模式的入口（給其他程式、AI 工作流用），放在 QuickSub.exe 旁邊。
// QuickSub.exe 是視窗程式：別的程式讀不到它的輸出，命令列也不會等它結束。
// 這個小程式用 --cli 啟動它、把 stdout/stderr 原封不動轉出來、等它跑完、傳回它的結束代碼。
// build_release.ps1 用 Windows 內建的 .NET Framework csc 編譯。
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;

static class QuickSubCli
{
    static void Pipe(Stream from, Stream to)
    {
        var buf = new byte[65536];
        int n;
        while ((n = from.Read(buf, 0, buf.Length)) > 0)
        {
            to.Write(buf, 0, n);
            to.Flush();
        }
    }

    // Windows 命令列的引號規則（反斜線和雙引號）
    static string Quote(string a)
    {
        if (a.Length > 0 && a.IndexOfAny(new[] { ' ', '\t', '"' }) < 0)
            return a;
        var sb = new StringBuilder("\"");
        int bs = 0;
        foreach (char c in a)
        {
            if (c == '\\') { bs++; continue; }
            if (c == '"') { sb.Append('\\', bs * 2 + 1).Append('"'); bs = 0; continue; }
            sb.Append('\\', bs).Append(c);
            bs = 0;
        }
        return sb.Append('\\', bs * 2).Append('"').ToString();
    }

    static int Main(string[] args)
    {
        string exe = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "QuickSub.exe");
        if (!File.Exists(exe))
        {
            Console.Error.WriteLine("QuickSub.exe not found next to quicksub-cli.exe");
            return 2;
        }
        var cmd = new StringBuilder("--cli");
        foreach (var a in args)
            cmd.Append(' ').Append(Quote(a));
        var psi = new ProcessStartInfo(exe, cmd.ToString())
        {
            UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true
        };
        var child = Process.Start(psi);
        Console.CancelKeyPress += (s, e) => { try { child.Kill(); } catch { } };
        var o = new Thread(() => Pipe(child.StandardOutput.BaseStream, Console.OpenStandardOutput()));
        var err = new Thread(() => Pipe(child.StandardError.BaseStream, Console.OpenStandardError()));
        o.Start();
        err.Start();
        child.WaitForExit();
        o.Join();
        err.Join();
        return child.ExitCode;
    }
}
