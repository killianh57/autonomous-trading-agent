    total_pnl = stats['total_pnl']
    if total_pnl > 0:
        report += f"\n🧾 <b>Si tu retires tes gains :</b>\n"
        for pct in [10, 25, 50, 100]:
            montant = total_pnl * pct / 100
            impot   = montant * 0.30
            net     = montant - impot
            report += f"{pct}% → ${montant:.2f} | impôt ~${impot:.2f} | net ~${net:.2f}\n"
        report += "\n"
    report += f"🤖 {'🏖️ Vacances' if vacation_mode else '⏸️ Pause' if trading_paused else '✅ Actif'}"
    send_telegram(report)
