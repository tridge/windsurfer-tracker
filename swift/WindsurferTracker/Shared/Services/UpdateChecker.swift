#if SIDELOAD
import Foundation

struct iOSVersionInfo {
    let version: String
    let buildNumber: Int
    let url: String
    let changelog: String
}

enum UpdateCheckResult {
    case updateAvailable(iOSVersionInfo)
    case noUpdate
    case error(String)
}

class UpdateChecker {
    static let versionURL = "https://wstracker.org/app/version.json"

    static func checkForUpdate() async -> UpdateCheckResult {
        guard let url = URL(string: versionURL) else {
            return .error("Invalid version URL")
        }

        do {
            let (data, _) = try await URLSession.shared.data(from: url)

            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let ios = json["ios"] as? [String: Any],
                  let version = ios["version"] as? String,
                  let buildNumber = ios["buildNumber"] as? Int,
                  let installURL = ios["url"] as? String else {
                return .error("Invalid version data from server")
            }

            let changelog = (ios["changelog"] as? String) ?? (json["changelog"] as? String) ?? ""

            guard let localBuildStr = Bundle.main.infoDictionary?["CFBundleVersion"] as? String,
                  let localBuild = Int(localBuildStr) else {
                return .error("Cannot determine current build number")
            }

            if buildNumber > localBuild {
                return .updateAvailable(iOSVersionInfo(
                    version: version,
                    buildNumber: buildNumber,
                    url: installURL,
                    changelog: changelog
                ))
            } else {
                return .noUpdate
            }
        } catch {
            return .error("Failed to check for updates: \(error.localizedDescription)")
        }
    }

    static var currentVersionString: String {
        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "1.0"
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "1"
        let gitHash = Bundle.main.infoDictionary?["GIT_HASH"] as? String

        if let hash = gitHash, !hash.isEmpty {
            return "\(version) (\(build)) \(hash)"
        } else {
            return "\(version) (\(build))"
        }
    }
}
#endif
