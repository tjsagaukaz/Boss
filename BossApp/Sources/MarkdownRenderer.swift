import SwiftUI

// MARK: - AST Model

enum MarkdownBlock: Hashable {
    case heading(level: Int, text: String)
    case paragraph(text: String)
    case blockquote(blocks: [MarkdownNode])
    case list(ordered: Bool, items: [[MarkdownNode]])
    case code(language: String?, code: String)
    case divider
}

struct MarkdownNode: Identifiable, Hashable {
    let id: String
    let block: MarkdownBlock
}

// MARK: - Recursive Parser

enum MarkdownParser {
    static func parse(_ text: String) -> [MarkdownNode] {
        let normalized = text
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        return assignIDs(to: parseLines(normalized.components(separatedBy: "\n")))
    }

    private struct ListMatch {
        let markerLength: Int
        let ordered: Bool
    }

    private struct ParsedList {
        let block: MarkdownBlock
        let nextIndex: Int
    }

    private static func assignIDs(to blocks: [MarkdownBlock], prefix: String = "root") -> [MarkdownNode] {
        blocks.enumerated().map { index, block in
            let id = "\(prefix)-\(index)"

            switch block {
            case .blockquote(let blocks):
                return MarkdownNode(
                    id: id,
                    block: .blockquote(blocks: assignIDs(to: blocks.map(\.block), prefix: "\(id)-quote"))
                )

            case .list(let ordered, let items):
                let nestedItems = items.enumerated().map { itemIndex, nodes in
                    assignIDs(to: nodes.map(\.block), prefix: "\(id)-item\(itemIndex)")
                }
                return MarkdownNode(id: id, block: .list(ordered: ordered, items: nestedItems))

            default:
                return MarkdownNode(id: id, block: block)
            }
        }
    }

    private static func parseLines(_ lines: [String]) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var index = 0

        while index < lines.count {
            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if trimmed.isEmpty {
                index += 1
                continue
            }

            if isDivider(trimmed) {
                blocks.append(.divider)
                index += 1
                continue
            }

            if let codeFence = parseCodeFenceStart(trimmed) {
                var codeLines: [String] = []
                index += 1

                while index < lines.count {
                    if lines[index].trimmingCharacters(in: .whitespaces).hasPrefix(codeFence.fence) {
                        index += 1
                        break
                    }
                    codeLines.append(lines[index])
                    index += 1
                }

                blocks.append(.code(language: codeFence.language, code: codeLines.joined(separator: "\n")))
                continue
            }

            if trimmed.hasPrefix(">") {
                var quoteLines: [String] = []

                while index < lines.count {
                    let quoteLine = lines[index]
                    let quoteTrimmed = quoteLine.trimmingCharacters(in: .whitespaces)

                    if quoteTrimmed.isEmpty {
                        quoteLines.append("")
                        index += 1
                        continue
                    }

                    guard quoteTrimmed.hasPrefix(">") else { break }
                    quoteLines.append(String(quoteTrimmed.dropFirst()).trimmingCharacters(in: .whitespaces))
                    index += 1
                }

                blocks.append(.blockquote(blocks: assignIDs(to: parseLines(quoteLines), prefix: "quote-\(blocks.count)")))
                continue
            }

            if let heading = parseHeading(trimmed) {
                blocks.append(.heading(level: heading.level, text: heading.text))
                index += 1
                continue
            }

            if let parsedList = parseList(lines, startIndex: index) {
                blocks.append(parsedList.block)
                index = parsedList.nextIndex
                continue
            }

            var paragraphLines: [String] = []
            while index < lines.count {
                let paragraphLine = lines[index]
                let paragraphTrimmed = paragraphLine.trimmingCharacters(in: .whitespaces)

                if paragraphTrimmed.isEmpty
                    || isDivider(paragraphTrimmed)
                    || parseCodeFenceStart(paragraphTrimmed) != nil
                    || paragraphTrimmed.hasPrefix(">")
                    || parseHeading(paragraphTrimmed) != nil
                    || parseListStart(paragraphTrimmed) != nil {
                    break
                }

                paragraphLines.append(paragraphLine.trimmingCharacters(in: .whitespaces))
                index += 1
            }

            if !paragraphLines.isEmpty {
                blocks.append(.paragraph(text: paragraphLines.joined(separator: "\n")))
            }
        }

        return blocks
    }

    private static func parseList(_ lines: [String], startIndex: Int) -> ParsedList? {
        let firstLine = lines[startIndex]
        let trimmed = firstLine.trimmingCharacters(in: .whitespaces)
        guard let firstMatch = parseListStart(trimmed) else { return nil }

        let ordered = firstMatch.ordered
        let baseIndent = leadingWhitespaceCount(in: firstLine)
        var items: [[MarkdownNode]] = []
        var currentItemLines = [String(trimmed.dropFirst(firstMatch.markerLength)).trimmingCharacters(in: .whitespaces)]
        var index = startIndex + 1

        while index < lines.count {
            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            let indent = leadingWhitespaceCount(in: line)

            if trimmed.isEmpty {
                if let nextIndex = nextNonEmptyLine(after: index, in: lines) {
                    let nextLine = lines[nextIndex]
                    let nextTrimmed = nextLine.trimmingCharacters(in: .whitespaces)
                    let nextIndent = leadingWhitespaceCount(in: nextLine)

                    if nextIndent > baseIndent || (nextIndent == baseIndent && parseListStart(nextTrimmed)?.ordered == ordered) {
                        currentItemLines.append("")
                        index += 1
                        continue
                    }
                }
                break
            }

            if let nextMatch = parseListStart(trimmed), indent == baseIndent, nextMatch.ordered == ordered {
                items.append(assignIDs(to: parseLines(currentItemLines), prefix: "list-\(startIndex)-item\(items.count)"))
                currentItemLines = [String(trimmed.dropFirst(nextMatch.markerLength)).trimmingCharacters(in: .whitespaces)]
                index += 1
                continue
            }

            guard indent > baseIndent else { break }

            let dropCount = min(line.count, max(baseIndent + 2, 0))
            currentItemLines.append(String(line.dropFirst(dropCount)))
            index += 1
        }

        if !currentItemLines.isEmpty {
            items.append(assignIDs(to: parseLines(currentItemLines), prefix: "list-\(startIndex)-item\(items.count)"))
        }

        return ParsedList(block: .list(ordered: ordered, items: items), nextIndex: index)
    }

    private static func isDivider(_ text: String) -> Bool {
        let significant = text.filter { !$0.isWhitespace }
        guard significant.count >= 3, let marker = significant.first else { return false }
        guard marker == "-" || marker == "*" || marker == "_" else { return false }
        return significant.allSatisfy { $0 == marker }
    }

    private static func parseCodeFenceStart(_ text: String) -> (fence: String, language: String?)? {
        let fence = text.prefix(while: { $0 == "`" })
        guard fence.count >= 3 else { return nil }
        let language = String(text.dropFirst(fence.count)).trimmingCharacters(in: .whitespaces)
        return (String(fence), language.isEmpty ? nil : language)
    }

    private static func parseHeading(_ text: String) -> (level: Int, text: String)? {
        let hashes = text.prefix(while: { $0 == "#" }).count
        guard hashes > 0, hashes <= 6, text.dropFirst(hashes).hasPrefix(" ") else { return nil }
        return (hashes, String(text.dropFirst(hashes + 1)))
    }

    private static func parseListStart(_ text: String) -> ListMatch? {
        if text.hasPrefix("- ") || text.hasPrefix("* ") || text.hasPrefix("+ ") || text.hasPrefix("• ") {
            return ListMatch(markerLength: 2, ordered: false)
        }

        guard let dotIndex = text.firstIndex(of: ".") else { return nil }
        let numberPart = text[text.startIndex..<dotIndex]
        guard !numberPart.isEmpty, numberPart.allSatisfy(\.isNumber), text[dotIndex...].hasPrefix(". ") else {
            return nil
        }
        return ListMatch(markerLength: text.distance(from: text.startIndex, to: dotIndex) + 2, ordered: true)
    }

    private static func leadingWhitespaceCount(in line: String) -> Int {
        line.prefix(while: { $0 == " " || $0 == "\t" }).count
    }

    private static func nextNonEmptyLine(after index: Int, in lines: [String]) -> Int? {
        var current = index + 1
        while current < lines.count {
            if !lines[current].trimmingCharacters(in: .whitespaces).isEmpty {
                return current
            }
            current += 1
        }
        return nil
    }
}

// MARK: - Typography

enum MDTypo {
    static let primaryText = Color.white.opacity(0.92)
    static let secondaryText = Color.white.opacity(0.55)
    static let tertiaryText = Color.white.opacity(0.35)
    static let bodySize: CGFloat = 15
    static let lineGap: CGFloat = 7
    static let tracking: CGFloat = -0.15
}

// MARK: - Recursive Renderer

struct MarkdownBlocksView: View {
    let nodes: [MarkdownNode]

    init(blocks: [MarkdownNode]) {
        self.nodes = blocks
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(nodes) { node in
                MarkdownNodeView(node: node)
            }
        }
    }
}

private struct MarkdownNodeView: View {
    let node: MarkdownNode

    var body: some View {
        switch node.block {
        case .heading(let level, let text):
            headingView(level: level, text: text)

        case .paragraph(let text):
            paragraphView(text)

        case .blockquote(let blocks):
            blockquoteView(blocks)

        case .list(let ordered, let items):
            listView(ordered: ordered, items: items)

        case .code(let language, let code):
            codeBlockView(language: language, code: code)

        case .divider:
            Rectangle()
                .fill(Color.white.opacity(0.06))
                .frame(height: 1)
                .padding(.vertical, 8)
        }
    }

    private func headingView(level: Int, text: String) -> some View {
        let style: (CGFloat, Font.Weight) = {
            switch level {
            case 1: return (22, .semibold)
            case 2: return (19, .semibold)
            case 3: return (17, .medium)
            default: return (15, .medium)
            }
        }()

        return Text(inlineMarkdown(text))
            .font(.system(size: style.0, weight: style.1))
            .tracking(-0.2)
            .foregroundColor(MDTypo.primaryText)
            .padding(.top, level <= 2 ? 6 : 2)
            .textSelection(.enabled)
    }

    private func paragraphView(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(text.components(separatedBy: "\n").enumerated()), id: \.offset) { _, line in
                Text(inlineMarkdown(line))
                    .font(.system(size: MDTypo.bodySize))
                    .tracking(MDTypo.tracking)
                    .lineSpacing(MDTypo.lineGap)
                    .foregroundColor(MDTypo.primaryText)
                    .textSelection(.enabled)
            }
        }
    }

    private func blockquoteView(_ blocks: [MarkdownNode]) -> some View {
        HStack(alignment: .top, spacing: 12) {
            RoundedRectangle(cornerRadius: 2)
                .fill(Color.white.opacity(0.12))
                .frame(width: 4)

            VStack(alignment: .leading, spacing: 10) {
                ForEach(blocks) { nested in
                    MarkdownNodeView(node: nested)
                }
            }
        }
        .padding(.leading, 2)
    }

    private func codeBlockView(language: String?, code: String) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                Circle().fill(Color.red.opacity(0.8)).frame(width: 8, height: 8)
                Circle().fill(Color.yellow.opacity(0.8)).frame(width: 8, height: 8)
                Circle().fill(Color.green.opacity(0.8)).frame(width: 8, height: 8)
                Spacer()

                if let language, !language.isEmpty {
                    Text(language.uppercased())
                        .font(.system(size: 10, weight: .bold, design: .monospaced))
                        .foregroundColor(MDTypo.tertiaryText)
                        .tracking(0.4)
                }
            }
            .padding(.horizontal, 12)
            .padding(.top, 10)
            .padding(.bottom, 8)
            .background(Color.white.opacity(0.025))

            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(.system(size: 13, design: .monospaced))
                    .foregroundColor(Color.white.opacity(0.82))
                    .lineSpacing(4)
                    .textSelection(.enabled)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 12)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(Color.white.opacity(0.035))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.white.opacity(0.05), lineWidth: 1)
        )
    }

    private func listView(ordered: Bool, items: [[MarkdownNode]]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(items.enumerated()), id: \.offset) { index, itemNodes in
                HStack(alignment: .top, spacing: 8) {
                    if let checkboxState = checkboxState(in: itemNodes) {
                        Image(systemName: checkboxState ? "checkmark.square.fill" : "square")
                            .foregroundColor(checkboxState ? Color.white.opacity(0.88) : MDTypo.secondaryText)
                            .padding(.top, 2)
                            .frame(width: 18, alignment: .center)
                    } else if ordered {
                        Text("\(index + 1).")
                            .font(.system(size: MDTypo.bodySize - 1, weight: .medium, design: .monospaced))
                            .foregroundColor(MDTypo.tertiaryText)
                            .frame(width: 24, alignment: .trailing)
                    } else {
                        Text("•")
                            .font(.system(size: MDTypo.bodySize + 1))
                            .foregroundColor(MDTypo.tertiaryText)
                            .frame(width: 16, alignment: .center)
                    }

                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(cleanCheckbox(from: itemNodes)) { nested in
                            MarkdownNodeView(node: nested)
                        }
                    }
                }
            }
        }
        .padding(.leading, 4)
    }

    private func inlineMarkdown(_ text: String) -> AttributedString {
        (try? AttributedString(
            markdown: text,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )) ?? AttributedString(text)
    }

    private func checkboxState(in nodes: [MarkdownNode]) -> Bool? {
        guard let first = nodes.first,
              case .paragraph(let text) = first.block else {
            return nil
        }

        let trimmed = text.trimmingCharacters(in: .whitespaces)
        if trimmed.hasPrefix("[ ]") { return false }
        if trimmed.hasPrefix("[x]") || trimmed.hasPrefix("[X]") { return true }
        return nil
    }

    private func cleanCheckbox(from nodes: [MarkdownNode]) -> [MarkdownNode] {
        guard let first = nodes.first,
              case .paragraph(let text) = first.block,
              checkboxState(in: nodes) != nil else {
            return nodes
        }

        let cleanedText = String(text.dropFirst(3)).trimmingCharacters(in: .whitespaces)
        var cleaned = nodes
        cleaned[0] = MarkdownNode(id: first.id, block: .paragraph(text: cleanedText))
        return cleaned
    }
}

// MARK: - Streaming Fallback

struct StreamingTextView: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.system(size: MDTypo.bodySize))
            .tracking(MDTypo.tracking)
            .lineSpacing(MDTypo.lineGap)
            .foregroundColor(MDTypo.primaryText)
            .textSelection(.enabled)
    }
}