import SwiftUI

struct PermissionPromptView: View {
    let request: PermissionRequest
    let onDecision: (PermissionDecision) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Boss wants to:")
                .font(.system(size: 11, weight: .medium))
                .foregroundColor(Color.white.opacity(0.45))

            Text(request.description)
                .font(.system(size: 14))
                .foregroundColor(Color.white.opacity(0.88))

            Text(request.scopeLabel)
                .font(.system(size: 11))
                .foregroundColor(Color.white.opacity(0.42))

            HStack(spacing: 8) {
                decisionButton(
                    title: "Allow Once",
                    foreground: .white,
                    background: Color.white.opacity(0.14)
                ) {
                    onDecision(.allowOnce)
                }

                decisionButton(
                    title: "Always Allow",
                    foreground: Color.white.opacity(0.82),
                    background: Color.white.opacity(0.08)
                ) {
                    onDecision(.alwaysAllow)
                }

                decisionButton(
                    title: "Deny",
                    foreground: Color.white.opacity(0.55),
                    background: Color.white.opacity(0.04)
                ) {
                    onDecision(.deny)
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(
            RoundedRectangle(cornerRadius: 14)
                .fill(Color.white.opacity(0.045))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .stroke(Color.white.opacity(0.06), lineWidth: 1)
        )
    }

    private func decisionButton(
        title: String,
        foreground: Color,
        background: Color,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Text(title)
                .font(.system(size: 12, weight: .medium))
                .foregroundColor(foreground)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(
                    RoundedRectangle(cornerRadius: 10)
                        .fill(background)
                )
        }
        .buttonStyle(.plain)
    }
}