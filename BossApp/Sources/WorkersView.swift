import SwiftUI

struct WorkersView: View {
    @EnvironmentObject var vm: ChatViewModel

    var body: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 24) {
                header
                    .padding(.top, 80)

                if let error = vm.workersRefreshError {
                    InlineStatusBanner(message: error)
                }

                controlsCard

                if !vm.workPlans.isEmpty {
                    plansSection
                }

                if let plan = vm.selectedWorkPlan {
                    planDetail(plan)
                } else {
                    emptyState
                }
            }
            .frame(maxWidth: 680, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.bottom, 32)
        }
        .task {
            await vm.refreshWorkersSurface()
        }
    }

    // MARK: - Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Workers")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(BossColor.textPrimary)

            Text("Monitor parallel task plans created from chat")
                .font(.system(size: 13))
                .foregroundColor(Color.white.opacity(0.38))
        }
    }

    // MARK: - Controls card

    private var controlsCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Work Plans")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundColor(Color.white.opacity(0.9))

                    Text("Plans are created from chat. Workers execute in isolated workspaces under execution governance.")
                        .font(.system(size: 12))
                        .foregroundColor(Color.white.opacity(0.34))
                }

                Spacer()

                Button(action: { Task { await vm.refreshWorkersSurface() } }) {
                    Text("Refresh")
                        .font(.system(size: 12))
                        .foregroundColor(Color.white.opacity(0.64))
                }
                .buttonStyle(.plain)
            }

            HStack(spacing: 24) {
                metric(label: "Plans", value: vm.workPlans.count)
                metric(label: "Running", value: vm.workPlans.filter { $0.status == "running" }.count)
                metric(label: "Workers", value: vm.workPlans.reduce(0) { $0 + $1.workers.count })
            }
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(Color.white.opacity(0.04))
                .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.white.opacity(0.06)))
        )
    }

    // MARK: - Plan list

    private var plansSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Recent Plans")
                .font(.system(size: 13, weight: .medium))
                .foregroundColor(Color.white.opacity(0.5))

            ForEach(vm.workPlans.prefix(12)) { plan in
                Button(action: { vm.selectedWorkPlan = plan }) {
                    HStack(spacing: 10) {
                        statusDot(plan.status)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(plan.task.prefix(80))
                                .font(.system(size: 13))
                                .foregroundColor(Color.white.opacity(0.8))
                                .lineLimit(1)
                            HStack(spacing: 6) {
                                Text(plan.status)
                                    .font(.system(size: 11))
                                    .foregroundColor(statusColor(plan.status))
                                Text("\(plan.workers.count) worker\(plan.workers.count == 1 ? "" : "s")")
                                    .font(.system(size: 11))
                                    .foregroundColor(Color.white.opacity(0.34))
                            }
                        }
                        Spacer()
                    }
                    .padding(.vertical, 6)
                    .padding(.horizontal, 10)
                    .background(
                        vm.selectedWorkPlan?.planId == plan.planId
                            ? Color.white.opacity(0.06)
                            : Color.clear
                    )
                    .cornerRadius(6)
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: - Plan detail

    private func planDetail(_ plan: WorkPlanInfo) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            // Header row
            HStack {
                Text(plan.task)
                    .font(.system(size: 14, weight: .medium))
                    .foregroundColor(Color.white.opacity(0.9))
                    .lineLimit(2)
                Spacer()
                statusPill(plan.status)
            }

            // Meta data
            VStack(alignment: .leading, spacing: 6) {
                metaRow(label: "Plan ID", value: String(plan.planId.prefix(12)))
                metaRow(label: "Merge Strategy", value: plan.mergeStrategy)
                metaRow(label: "Max Concurrent", value: "\(plan.maxConcurrent)")
                if let err = plan.error {
                    metaRow(label: "Error", value: err)
                }
            }

            // Workers
            if !plan.workers.isEmpty {
                workersSection(plan.workers)
            }

            // Merge summary
            if !plan.mergeSummary.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Merge Summary")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundColor(Color.white.opacity(0.5))
                    Text(plan.mergeSummary)
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundColor(Color.white.opacity(0.6))
                        .lineLimit(20)
                }
            }

            // Actions
            if plan.status == "running" || plan.status == "ready" {
                HStack(spacing: 12) {
                    if plan.status == "running" {
                        Button("Cancel") {
                            Task {
                                do {
                                    _ = try await APIClient.shared.cancelWorkPlan(planId: plan.planId)
                                    await vm.refreshWorkersSurface()
                                } catch {}
                            }
                        }
                        .buttonStyle(.plain)
                        .foregroundColor(.red.opacity(0.8))
                        .font(.system(size: 12))
                    }
                }
            }
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(Color.white.opacity(0.04))
                .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.white.opacity(0.06)))
        )
    }

    // MARK: - Workers section

    private func workersSection(_ workers: [WorkerInfo]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Workers")
                .font(.system(size: 12, weight: .medium))
                .foregroundColor(Color.white.opacity(0.5))

            ForEach(workers) { worker in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        statusDot(worker.state)
                        Text(worker.role.capitalized)
                            .font(.system(size: 12, weight: .medium))
                            .foregroundColor(roleColor(worker.role))
                        Text(worker.scope)
                            .font(.system(size: 12))
                            .foregroundColor(Color.white.opacity(0.6))
                            .lineLimit(1)
                        Spacer()
                        Text(worker.state)
                            .font(.system(size: 11))
                            .foregroundColor(statusColor(worker.state))
                    }

                    if !worker.fileTargets.isEmpty {
                        Text("Files: \(worker.fileTargets.joined(separator: ", "))")
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundColor(Color.white.opacity(0.34))
                            .lineLimit(1)
                    }

                    if let err = worker.error {
                        Text(err)
                            .font(.system(size: 11))
                            .foregroundColor(.red.opacity(0.7))
                            .lineLimit(2)
                    }

                    if !worker.resultSummary.isEmpty {
                        Text(worker.resultSummary.prefix(200))
                            .font(.system(size: 11))
                            .foregroundColor(Color.white.opacity(0.5))
                            .lineLimit(3)
                    }
                }
                .padding(8)
                .background(Color.white.opacity(0.02))
                .cornerRadius(6)
            }
        }
    }

    // MARK: - Empty state

    private var emptyState: some View {
        VStack(spacing: 8) {
            Text("No plan selected")
                .font(.system(size: 14))
                .foregroundColor(Color.white.opacity(0.38))
            Text("Select a work plan from the list above, or create one from chat.")
                .font(.system(size: 12))
                .foregroundColor(Color.white.opacity(0.24))
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
    }

    // MARK: - Helpers

    private func metric(label: String, value: Int) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("\(value)")
                .font(.system(size: 16, weight: .semibold, design: .monospaced))
                .foregroundColor(Color.white.opacity(0.8))
            Text(label)
                .font(.system(size: 11))
                .foregroundColor(Color.white.opacity(0.34))
        }
    }

    private func statusDot(_ status: String) -> some View {
        Circle()
            .fill(statusColor(status))
            .frame(width: 6, height: 6)
    }

    private func statusPill(_ status: String) -> some View {
        Text(status.replacingOccurrences(of: "_", with: " "))
            .font(.system(size: 11, weight: .medium))
            .foregroundColor(statusColor(status))
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(statusColor(status).opacity(0.12))
            .cornerRadius(4)
    }

    private func statusColor(_ status: String) -> Color {
        switch status {
        case "running", "merging": return .blue.opacity(0.8)
        case "completed": return .green.opacity(0.7)
        case "failed": return .red.opacity(0.7)
        case "cancelled": return .orange.opacity(0.7)
        case "ready": return .cyan.opacity(0.7)
        case "pending", "planning": return Color.white.opacity(0.4)
        default: return Color.white.opacity(0.38)
        }
    }

    private func roleColor(_ role: String) -> Color {
        switch role {
        case "explorer": return .cyan.opacity(0.8)
        case "implementer": return .orange.opacity(0.8)
        case "reviewer": return .green.opacity(0.8)
        default: return Color.white.opacity(0.6)
        }
    }

    private func metaRow(label: String, value: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text(label)
                .font(.system(size: 12))
                .foregroundColor(Color.white.opacity(0.38))
                .frame(width: 100, alignment: .trailing)
            Text(value)
                .font(.system(size: 12))
                .foregroundColor(Color.white.opacity(0.64))
                .textSelection(.enabled)
        }
    }
}
