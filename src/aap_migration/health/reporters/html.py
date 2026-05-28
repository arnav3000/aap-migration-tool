"""HTML health check reporter."""

import html
from datetime import datetime

from aap_migration.health.models import CheckStatus, HealthCheckReport, Severity


class HTMLReporter:
    """Generate HTML health check reports."""

    @staticmethod
    def generate(report: HealthCheckReport) -> str:
        """Generate HTML report.

        Args:
            report: Health check report

        Returns:
            HTML string
        """
        # Generate sections
        summary_html = HTMLReporter._generate_summary(report)
        critical_html = HTMLReporter._generate_critical_section(report)
        warning_html = HTMLReporter._generate_warning_section(report)
        info_html = HTMLReporter._generate_info_section(report)
        passed_html = HTMLReporter._generate_passed_section(report)

        # Combine into full HTML
        html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AAP Pre-Migration Health Check Report</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            line-height: 1.6;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        h1, h2, h3 {{
            color: #333;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 8px;
            margin-bottom: 30px;
        }}
        .header h1 {{
            margin: 0;
            color: white;
        }}
        .meta {{
            opacity: 0.9;
            margin-top: 10px;
        }}
        .summary {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 20px;
        }}
        .summary-card {{
            padding: 15px;
            border-radius: 6px;
            text-align: center;
        }}
        .summary-card.critical {{
            background-color: #fee;
            border-left: 4px solid #dc3545;
        }}
        .summary-card.warning {{
            background-color: #fff3cd;
            border-left: 4px solid #ffc107;
        }}
        .summary-card.info {{
            background-color: #d1ecf1;
            border-left: 4px solid #17a2b8;
        }}
        .summary-card.pass {{
            background-color: #d4edda;
            border-left: 4px solid #28a745;
        }}
        .summary-card h3 {{
            margin: 0 0 10px 0;
            font-size: 14px;
            text-transform: uppercase;
            opacity: 0.8;
        }}
        .summary-card .count {{
            font-size: 36px;
            font-weight: bold;
        }}
        .readiness {{
            text-align: center;
            margin: 20px 0;
        }}
        .readiness-meter {{
            background-color: #eee;
            height: 30px;
            border-radius: 15px;
            overflow: hidden;
            margin: 10px 0;
        }}
        .readiness-fill {{
            height: 100%;
            background: linear-gradient(90deg, #dc3545 0%, #ffc107 50%, #28a745 100%);
            transition: width 0.3s ease;
        }}
        .section {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .check-item {{
            border-left: 4px solid #ddd;
            padding: 15px;
            margin: 15px 0;
            background-color: #f9f9f9;
            border-radius: 4px;
        }}
        .check-item.critical {{
            border-left-color: #dc3545;
            background-color: #fee;
        }}
        .check-item.warning {{
            border-left-color: #ffc107;
            background-color: #fff3cd;
        }}
        .check-item.info {{
            border-left-color: #17a2b8;
            background-color: #d1ecf1;
        }}
        .check-item.pass {{
            border-left-color: #28a745;
            background-color: #d4edda;
        }}
        .check-item h3 {{
            margin-top: 0;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .badge {{
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
            text-transform: uppercase;
        }}
        .badge.critical {{
            background-color: #dc3545;
            color: white;
        }}
        .badge.warning {{
            background-color: #ffc107;
            color: #333;
        }}
        .badge.info {{
            background-color: #17a2b8;
            color: white;
        }}
        .badge.pass {{
            background-color: #28a745;
            color: white;
        }}
        .recommendation {{
            background-color: #e7f3ff;
            border: 1px solid #b3d9ff;
            padding: 15px;
            border-radius: 4px;
            margin-top: 10px;
            white-space: pre-wrap;
        }}
        .details {{
            margin-top: 15px;
            padding: 15px;
            background-color: #fff;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-family: monospace;
            font-size: 13px;
            overflow-x: auto;
        }}
        code {{
            background-color: #f4f4f4;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: 'Courier New', monospace;
        }}
        ul {{
            padding-left: 20px;
        }}
        .footer {{
            text-align: center;
            padding: 20px;
            color: #666;
            font-size: 14px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🏥 AAP Pre-Migration Health Check Report</h1>
        <div class="meta">
            <div><strong>Source:</strong> {html.escape(report.source_url)}</div>
            <div><strong>Generated:</strong> {report.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}</div>
        </div>
    </div>

    {summary_html}
    {critical_html}
    {warning_html}
    {info_html}
    {passed_html}

    <div class="footer">
        <p>Generated by <strong>aap-bridge health-check</strong></p>
    </div>
</body>
</html>
"""
        return html_content

    @staticmethod
    def _generate_summary(report: HealthCheckReport) -> str:
        """Generate summary section."""
        readiness = report.migration_readiness
        readiness_color = "#dc3545" if readiness < 50 else "#ffc107" if readiness < 80 else "#28a745"

        return f"""
    <div class="summary">
        <h2>Summary</h2>
        <div class="summary-grid">
            <div class="summary-card critical">
                <h3>Critical Issues</h3>
                <div class="count">{report.summary['critical']}</div>
                <div>Must Fix</div>
            </div>
            <div class="summary-card warning">
                <h3>Warnings</h3>
                <div class="count">{report.summary['warning']}</div>
                <div>Should Fix</div>
            </div>
            <div class="summary-card info">
                <h3>Info</h3>
                <div class="count">{report.summary['info']}</div>
                <div>FYI</div>
            </div>
            <div class="summary-card pass">
                <h3>Passed</h3>
                <div class="count">{report.summary['passed']}</div>
                <div>OK</div>
            </div>
        </div>
        <div class="readiness">
            <h3>Migration Readiness</h3>
            <div class="readiness-meter">
                <div class="readiness-fill" style="width: {readiness}%; background-color: {readiness_color};"></div>
            </div>
            <div style="font-size: 24px; font-weight: bold; color: {readiness_color};">{readiness:.1f}%</div>
            <div>{'✅ Ready for migration' if report.is_migration_ready else '❌ Fix critical issues before migration'}</div>
        </div>
    </div>
"""

    @staticmethod
    def _generate_critical_section(report: HealthCheckReport) -> str:
        """Generate critical issues section."""
        critical_results = [r for r in report.results if r.is_critical]

        if not critical_results:
            return ""

        items_html = ""
        for result in critical_results:
            items_html += f"""
        <div class="check-item critical">
            <h3>
                <span class="badge critical">CRITICAL</span>
                {html.escape(result.check_name.replace('_', ' ').title())}
            </h3>
            <p><strong>{html.escape(result.message)}</strong></p>
            <p>Count: <strong>{result.count}</strong> affected resources</p>
            {f'<div class="recommendation"><strong>Recommendation:</strong><br>{html.escape(result.recommendation)}</div>' if result.recommendation else ''}
            {HTMLReporter._format_details(result.details) if result.details else ''}
        </div>
"""

        return f"""
    <div class="section">
        <h2>❌ Critical Issues (Must Fix Before Migration)</h2>
        {items_html}
    </div>
"""

    @staticmethod
    def _generate_warning_section(report: HealthCheckReport) -> str:
        """Generate warnings section."""
        warning_results = [r for r in report.results if r.is_warning]

        if not warning_results:
            return ""

        items_html = ""
        for result in warning_results:
            items_html += f"""
        <div class="check-item warning">
            <h3>
                <span class="badge warning">WARNING</span>
                {html.escape(result.check_name.replace('_', ' ').title())}
            </h3>
            <p><strong>{html.escape(result.message)}</strong></p>
            <p>Count: <strong>{result.count}</strong> affected resources</p>
            {f'<div class="recommendation"><strong>Recommendation:</strong><br>{html.escape(result.recommendation)}</div>' if result.recommendation else ''}
        </div>
"""

        return f"""
    <div class="section">
        <h2>⚠️  Warnings (Should Fix)</h2>
        {items_html}
    </div>
"""

    @staticmethod
    def _generate_info_section(report: HealthCheckReport) -> str:
        """Generate info section."""
        info_results = [
            r
            for r in report.results
            if r.severity == Severity.INFO and r.status == CheckStatus.FAIL
        ]

        if not info_results:
            return ""

        items_html = ""
        for result in info_results:
            items_html += f"""
        <div class="check-item info">
            <h3>
                <span class="badge info">INFO</span>
                {html.escape(result.check_name.replace('_', ' ').title())}
            </h3>
            <p>{html.escape(result.message)}</p>
        </div>
"""

        return f"""
    <div class="section">
        <h2>ℹ️  Information</h2>
        {items_html}
    </div>
"""

    @staticmethod
    def _generate_passed_section(report: HealthCheckReport) -> str:
        """Generate passed checks section."""
        passed_results = [r for r in report.results if r.is_pass]

        if not passed_results:
            return ""

        items_html = ""
        for result in passed_results:
            items_html += f"""
        <div class="check-item pass">
            <h3>
                <span class="badge pass">PASS</span>
                {html.escape(result.check_name.replace('_', ' ').title())}
            </h3>
            <p>{html.escape(result.message)}</p>
        </div>
"""

        return f"""
    <div class="section">
        <h2>✅ Passed Checks</h2>
        {items_html}
    </div>
"""

    @staticmethod
    def _format_details(details: dict) -> str:
        """Format details dict as HTML.

        Args:
            details: Details dictionary

        Returns:
            HTML string
        """
        import json

        details_json = json.dumps(details, indent=2)
        return f'<div class="details"><pre>{html.escape(details_json)}</pre></div>'
